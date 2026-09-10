"""Generate videos through the MoneyPrinterTurbo REST API.

Flow per topic:
  POST /api/v1/videos -> task_id
  GET  /api/v1/tasks/{task_id} until state == 1 (complete) or -1 (failed)
  GET  <videos[0]> (e.g. /tasks/<task_id>/final-1.mp4) -> output/pending/<topic_id>.mp4
  write sidecar JSON -> output/pending/<topic_id>.json   (see review.py)

CLI: python -m orchestrator.generate [--dry-run] [--count N] [--topic ID]
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

from orchestrator import config, review
from orchestrator.models import VideoRequest, VideoResult

log = logging.getLogger(__name__)

TASK_STATE_FAILED = -1
TASK_STATE_COMPLETE = 1
TASK_STATE_PROCESSING = 4


class GenerationError(RuntimeError):
    pass


class MPTClient:
    """Thin client for the MoneyPrinterTurbo API (black box, REST only)."""

    def __init__(self, settings: config.Settings, session: requests.Session | None = None):
        self.base = settings.mpt_base_url.rstrip("/")
        self.session = session or requests.Session()
        if settings.mpt_api_key:
            self.session.headers["x-api-key"] = settings.mpt_api_key
        if settings.basic_auth_user and settings.basic_auth_password:
            self.session.auth = (settings.basic_auth_user, settings.basic_auth_password)

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base}/{path_or_url.lstrip('/')}"

    def _json(self, resp: requests.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or body.get("status", resp.status_code) != 200:
            raise GenerationError(
                f"MPT {resp.request.method} {resp.url} -> HTTP {resp.status_code}: "
                f"{body.get('message') or resp.text[:300]}"
            )
        return body

    def ping(self) -> bool:
        return self.session.get(self._url("/ping"), timeout=10).status_code == 200

    def create_video(self, payload: dict[str, Any]) -> str:
        resp = self.session.post(self._url("/api/v1/videos"), json=payload, timeout=60)
        task_id = (self._json(resp).get("data") or {}).get("task_id")
        if not task_id:
            raise GenerationError("MPT did not return a task_id")
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        resp = self.session.get(self._url(f"/api/v1/tasks/{task_id}"), timeout=30)
        return self._json(resp).get("data") or {}

    def wait_for_task(
        self,
        task_id: str,
        poll_interval_s: float,
        timeout_s: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        deadline = clock() + timeout_s
        last_progress = None
        while True:
            task = self.get_task(task_id)
            state, progress = task.get("state"), task.get("progress", 0)
            if progress != last_progress:
                log.info("task %s: state=%s progress=%s%%", task_id, state, progress)
                last_progress = progress
            if state == TASK_STATE_COMPLETE:
                return task
            if state == TASK_STATE_FAILED:
                raise GenerationError(
                    f"task {task_id} failed at stage {task.get('failed_stage')!r}: {task.get('error')}"
                )
            if clock() >= deadline:
                raise TimeoutError(f"task {task_id} not complete after {timeout_s:.0f}s")
            sleep(poll_interval_s)

    def social_metadata(self, subject: str, script: str) -> dict[str, Any]:
        resp = self.session.post(
            self._url("/api/v1/social-metadata"),
            json={"video_subject": subject, "video_script": script[:8000],
                  "language": "auto", "platform": "youtube"},
            timeout=120,
        )
        return self._json(resp).get("data") or {}

    def download(self, uri: str, dest: Path) -> Path:
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.session.get(self._url(uri), stream=True, timeout=300) as resp:
            if resp.status_code != 200:
                raise GenerationError(f"download {uri} -> HTTP {resp.status_code}")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        tmp.replace(dest)
        return dest


# --- topic queue ---------------------------------------------------------------

def load_queue(path: Path) -> list[VideoRequest]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    out = []
    for raw in data.get("topics") or []:
        merged = {**defaults, **raw}
        merged.setdefault("topic_id", merged.pop("id", None))
        merged.pop("id", None)
        if merged.get("enabled", True) is False:
            continue
        merged.pop("enabled", None)
        out.append(VideoRequest.from_dict(merged))
    ids = [r.topic_id for r in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate topic ids in {path}: {sorted(dupes)}")
    return out


def _state_path(settings: config.Settings) -> Path:
    return settings.output_dir / "queue_state.json"


def load_state(settings: config.Settings) -> dict[str, Any]:
    p = _state_path(settings)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save_state(settings: config.Settings, state: dict[str, Any]) -> None:
    p = _state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def next_topics(queue: list[VideoRequest], state: dict[str, Any], count: int) -> list[VideoRequest]:
    """Queue file is read-only for the pipeline; progress lives in output/queue_state.json."""
    return [r for r in queue if state.get(r.topic_id, {}).get("status") != "generated"][:count]


# --- pipeline -------------------------------------------------------------------

def generate_one(
    req: VideoRequest, settings: config.Settings, client: MPTClient | None, dry_run: bool = False
) -> Path | None:
    payload = req.to_mpt_payload()
    if dry_run:
        log.info("DRY-RUN: would POST %s/api/v1/videos with %s",
                 settings.mpt_base_url, json.dumps(payload, ensure_ascii=False))
        log.info("DRY-RUN: would poll /api/v1/tasks/<id>, download mp4 to %s/%s.mp4 and write sidecar",
                 settings.output_dir / review.PENDING, req.topic_id)
        return None
    assert client is not None
    task_id = client.create_video(payload)
    log.info("topic %s -> MPT task %s", req.topic_id, task_id)
    task = client.wait_for_task(task_id, settings.poll_interval_s, settings.generate_timeout_s)
    videos = task.get("videos") or []
    if not videos:
        raise GenerationError(f"task {task_id} completed without videos")

    pending = review.stage_dir(settings.output_dir, review.PENDING)
    mp4 = client.download(videos[0], pending / f"{req.topic_id}.mp4")
    script = task.get("script") or ""
    terms = task.get("terms") or []

    if not req.title or not req.description:
        try:
            meta = client.social_metadata(req.subject, script)
            req.title = req.title or meta.get("title", "")
            if not req.description:
                tags = " ".join(meta.get("hashtags") or [])
                req.description = f"{meta.get('caption', '')}\n\n{tags}".strip()
        except (GenerationError, requests.RequestException) as exc:
            log.warning("social-metadata failed (%s); falling back to subject", exc)
    req.title = req.title or req.subject

    result = VideoResult(request=req, task_id=task_id, video_path=str(mp4),
                         script=script, terms=list(terms) if isinstance(terms, list) else [])
    review.stage_pending(result, settings.output_dir)
    return mp4


def run(count: int | None = None, topic_id: str | None = None, dry_run: bool = False,
        settings: config.Settings | None = None, client: MPTClient | None = None) -> list[Path]:
    settings = settings or config.load()
    settings.require("generate", dry_run=dry_run)
    queue = load_queue(settings.queue_path)
    state = load_state(settings)
    if topic_id:
        todo = [r for r in queue if r.topic_id == topic_id]
        if not todo:
            raise SystemExit(f"topic {topic_id!r} not found in {settings.queue_path}")
    else:
        todo = next_topics(queue, state, count or settings.daily_generate_count)
    if not todo:
        log.info("topic queue is empty - nothing to generate")
        return []
    if not dry_run and client is None:
        client = MPTClient(settings)

    produced = []
    for req in todo:
        try:
            mp4 = generate_one(req, settings, client, dry_run=dry_run)
        except (GenerationError, TimeoutError, requests.RequestException) as exc:
            log.error("topic %s failed: %s", req.topic_id, exc)
            if not dry_run:
                state[req.topic_id] = {"status": "failed", "error": str(exc)[:500], "at": review.now_iso()}
                save_state(settings, state)
            continue
        if mp4 is not None:
            produced.append(mp4)
            state[req.topic_id] = {"status": "generated", "at": review.now_iso()}
            save_state(settings, state)
    return produced


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.generate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="log the requests without calling any API")
    p.add_argument("--count", type=int, help="number of queued topics to generate (default DAILY_GENERATE_COUNT)")
    p.add_argument("--topic", help="generate one specific topic id")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)
    run(count=args.count, topic_id=args.topic, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
