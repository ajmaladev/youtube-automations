"""Human approval gate.

    output/pending/   <- generate.py writes <topic_id>.mp4 + <topic_id>.json here
    output/approved/  <- YOU move items here (by hand or `review approve <id>`)
    output/rejected/  <- `review reject <id>`
    output/uploaded/  <- upload.py moves items here after a successful upload

Nothing is uploaded unless it is in output/approved/ AND its sha256 still
matches the hash recorded at generation time.

CLI: python -m orchestrator.review [--dry-run] {list,show,approve,reject} [topic_id]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import config
from orchestrator.models import VideoResult

log = logging.getLogger(__name__)

PENDING, APPROVED, REJECTED, UPLOADED = "pending", "approved", "rejected", "uploaded"
STAGES = (PENDING, APPROVED, REJECTED, UPLOADED)


def stage_dir(output_dir: Path, stage: str) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    d = Path(output_dir) / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_sidecar(path: Path, result: VideoResult) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_sidecar(path: Path) -> VideoResult:
    return VideoResult.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def video_file(sidecar: Path, result: VideoResult) -> Path:
    """Sidecars store the video by basename so items can be moved as a pair."""
    return Path(sidecar).parent / Path(result.video_path).name


def stage_pending(result: VideoResult, output_dir: Path) -> Path:
    """Record a freshly rendered video (already in pending/) with its sidecar."""
    pending = stage_dir(output_dir, PENDING)
    mp4 = Path(result.video_path)
    if mp4.parent.resolve() != pending.resolve():
        raise ValueError(f"video must already be in {pending}, got {mp4}")
    result.video_path = mp4.name
    result.sha256 = sha256_file(mp4)
    result.created_at = result.created_at or now_iso()
    sidecar = pending / f"{result.request.topic_id}.json"
    _write_sidecar(sidecar, result)
    log.info("staged for review: %s", sidecar)
    return sidecar


def list_items(output_dir: Path, stage: str) -> list[tuple[Path, VideoResult]]:
    items = []
    for sidecar in sorted(stage_dir(output_dir, stage).glob("*.json")):
        try:
            items.append((sidecar, read_sidecar(sidecar)))
        except (ValueError, KeyError, TypeError) as exc:
            log.error("skipping unreadable sidecar %s: %s", sidecar, exc)
    return items


def _move_pair(topic_id: str, output_dir: Path, src: str, dst: str, dry_run: bool) -> Path:
    sidecar = stage_dir(output_dir, src) / f"{topic_id}.json"
    if not sidecar.is_file():
        raise FileNotFoundError(f"no {src} item with id {topic_id!r}")
    result = read_sidecar(sidecar)
    mp4 = video_file(sidecar, result)
    dest_dir = stage_dir(output_dir, dst)
    if dry_run:
        log.info("DRY-RUN: would move %s and %s -> %s", mp4.name, sidecar.name, dest_dir)
        return dest_dir / sidecar.name
    if mp4.is_file():
        shutil.move(str(mp4), dest_dir / mp4.name)
    shutil.move(str(sidecar), dest_dir / sidecar.name)
    log.info("%s -> %s: %s", src, dst, topic_id)
    return dest_dir / sidecar.name


def approve(topic_id: str, output_dir: Path, dry_run: bool = False) -> Path:
    return _move_pair(topic_id, output_dir, PENDING, APPROVED, dry_run)


def reject(topic_id: str, output_dir: Path, dry_run: bool = False) -> Path:
    return _move_pair(topic_id, output_dir, PENDING, REJECTED, dry_run)


def load_approved(output_dir: Path) -> list[tuple[Path, VideoResult]]:
    """The ONLY source of uploadable items. Verifies file presence and hash."""
    ok = []
    for sidecar, result in list_items(output_dir, APPROVED):
        mp4 = video_file(sidecar, result)
        if not mp4.is_file():
            log.error("approved item %s has no video file %s; skipping", sidecar.name, mp4.name)
            continue
        if result.sha256 and sha256_file(mp4) != result.sha256:
            log.error("sha256 mismatch for %s (file changed after review); skipping", mp4.name)
            continue
        ok.append((sidecar, result))
    return ok


def mark_uploaded(
    sidecar: Path, result: VideoResult, video_id: str, output_dir: Path, dry_run: bool = False
) -> Path:
    if dry_run:
        log.info("DRY-RUN: would mark %s uploaded and move to %s/", sidecar.name, UPLOADED)
        return sidecar
    result.youtube_video_id = video_id
    result.uploaded_at = now_iso()
    _write_sidecar(sidecar, result)
    return _move_pair(result.request.topic_id, output_dir, APPROVED, UPLOADED, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.review", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="log moves without touching files")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "approve", "reject"])
    p.add_argument("topic_id", nargs="?")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)
    settings = config.load()
    out = settings.output_dir

    if args.action == "list":
        for stage in STAGES:
            items = list_items(out, stage)
            print(f"{stage} ({len(items)})")
            for _, r in items:
                print(f"  {r.request.topic_id:30} {r.request.title or r.request.subject}")
        return 0
    if not args.topic_id:
        p.error(f"{args.action} requires a topic_id")
    if args.action == "show":
        for stage in STAGES:
            sidecar = stage_dir(out, stage) / f"{args.topic_id}.json"
            if sidecar.is_file():
                print(f"[{stage}] {sidecar}")
                print(sidecar.read_text(encoding="utf-8"))
                return 0
        print(f"not found: {args.topic_id}")
        return 1
    fn = approve if args.action == "approve" else reject
    fn(args.topic_id, out, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
