"""Generate videos through the video engine's REST API.

What gets generated (in this order, up to --count videos):
  1. unfinished manual topics from topics/queue.yaml
  2. content-calendar episodes due up to CALENDAR_LOOKAHEAD_DAYS ahead (topics/calendar/, see content_calendar.py)
  3. unfinished episodes of already-planned series (output/series/*.json)
  4. brand-new series planned by the story LLM when AUTO_SERIES=true, only for days the calendar doesn't cover

Flow per video:
  POST /api/v1/videos -> task_id
  GET  /api/v1/tasks/{task_id} until state == 1 (complete) or -1 (failed)
  GET  <videos[0]> (e.g. /tasks/<task_id>/final-1.mp4) -> output/pending/<topic_id>.mp4
  write sidecar JSON -> output/pending/<topic_id>.json   (see review.py)
Every render is checked (video_check.py) and tried up to GENERATE_ATTEMPTS times before it counts as failed;
the last attempt widens the stock-footage search.

CLI: python -m orchestrator.generate [--dry-run] [--count N] [--topic ID] [--date YYYY-MM-DD [--part p1|p2|p3]]
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

from orchestrator import config, content_calendar, review, story, video_check
from orchestrator.llm import LLMError
from orchestrator.models import VideoRequest, VideoResult

log = logging.getLogger(__name__)

TASK_STATE_FAILED = -1
TASK_STATE_COMPLETE = 1
TASK_STATE_PROCESSING = 4
# added to the stock-footage search on a render's last attempt, so a thin Pexels result can't sink it
FALLBACK_TERMS = ("city lights at night", "clouds timelapse", "ocean waves slow motion", "forest aerial view",
                  "light rays in darkness")


class GenerationError(RuntimeError):
    pass


RETRIABLE_ERRORS = (GenerationError, TimeoutError, requests.RequestException)


class EngineClient:
    """Thin client for the video engine API (black box, REST only)."""

    def __init__(self, settings: config.Settings, session: requests.Session | None = None):
        self.base = settings.engine_base_url.rstrip("/")
        self.session = session or requests.Session()
        if settings.engine_api_key:
            self.session.headers["x-api-key"] = settings.engine_api_key
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
                f"Engine {resp.request.method} {resp.url} -> HTTP {resp.status_code}: "
                f"{body.get('message') or resp.text[:300]}"
            )
        return body

    def ping(self) -> bool:
        return self.session.get(self._url("/ping"), timeout=10).status_code == 200

    def create_video(self, payload: dict[str, Any]) -> str:
        resp = self.session.post(self._url("/api/v1/videos"), json=payload, timeout=60)
        task_id = (self._json(resp).get("data") or {}).get("task_id")
        if not task_id:
            raise GenerationError("Engine did not return a task_id")
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

def load_defaults(path: Path) -> dict[str, Any]:
    data = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}) if Path(path).is_file() else {}
    return dict(data.get("defaults") or {})


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


def select_topics(settings: config.Settings, queue: list[VideoRequest], defaults: dict[str, Any],
                  state: dict[str, Any], count: int, dry_run: bool = False, llm=None) -> list[VideoRequest]:
    """Manual queue first, then due calendar episodes, then unfinished series episodes, then brand-new series."""
    todo = next_topics(queue, state, count)
    if len(todo) < count:
        todo += content_calendar.due_episodes(settings, state, defaults)[: count - len(todo)]
    if len(todo) < count:
        todo += story.pending_episodes(settings, state, defaults)[: count - len(todo)]
    auto_series = settings.auto_series and not content_calendar.covers(settings)  # never invent a calendar day
    planned = 0
    while len(todo) < count and auto_series and planned < count:
        try:
            new = story.plan_and_save(settings, defaults, dry_run=dry_run, llm=llm)
        except (story.StoryError, LLMError) as exc:
            log.error("series planning failed: %s", exc)
            break
        if not new:
            break
        planned += 1
        todo += new[: count - len(todo)]  # leftover episodes are picked up by the next run
    return todo


# --- pipeline -------------------------------------------------------------------

def with_fallback_terms(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("video_terms")
    if not raw:
        return payload  # the engine picks its own terms; don't override them
    terms = [str(t).strip() for t in (raw if isinstance(raw, list) else str(raw).split(",")) if str(t).strip()]
    return {**payload, "video_terms": ", ".join(dict.fromkeys([*terms, *FALLBACK_TERMS]))}


def render_video(req: VideoRequest, payload: dict[str, Any], settings: config.Settings,
                 client: EngineClient) -> tuple[Path, str, dict[str, Any]]:
    """One engine render and download; a video that fails the checks is deleted and raises."""
    task_id = client.create_video(payload)
    log.info("topic %s -> engine task %s", req.topic_id, task_id)
    task = client.wait_for_task(task_id, settings.poll_interval_s, settings.generate_timeout_s)
    videos = task.get("videos") or []
    if not videos:
        raise GenerationError(f"task {task_id} completed without videos")

    pending = review.stage_dir(settings.output_dir, review.PENDING)
    mp4 = client.download(videos[0], pending / f"{req.topic_id}.mp4")
    if settings.verify_videos:
        words = len(str(payload.get("video_script") or task.get("script") or "").split())
        try:
            info = video_check.check_video(mp4, words=words, aspect=req.video_aspect)
        except video_check.VideoCheckError as exc:
            mp4.unlink(missing_ok=True)
            raise GenerationError(f"task {task_id} produced a broken video: {exc}") from exc
        log.info("topic %s: video checked (%.1fs, %dx%d, with audio)", req.topic_id, info.duration_s,
                 *info.frame_size)
    return mp4, task_id, task


def generate_one(
    req: VideoRequest, settings: config.Settings, client: EngineClient | None, dry_run: bool = False
) -> Path | None:
    payload = req.to_engine_payload()
    if dry_run:
        log.info("DRY-RUN: would POST %s/api/v1/videos with %s",
                 settings.engine_base_url, json.dumps(payload, ensure_ascii=False))
        log.info("DRY-RUN: would poll /api/v1/tasks/<id>, download mp4 to %s/%s.mp4 and write sidecar",
                 settings.output_dir / review.PENDING, req.topic_id)
        return None
    assert client is not None
    attempts = max(1, settings.generate_attempts)
    for attempt in range(1, attempts + 1):
        body = with_fallback_terms(payload) if attempt == attempts > 1 else payload
        try:
            mp4, task_id, task = render_video(req, body, settings, client)
            break
        except RETRIABLE_ERRORS as exc:
            if attempt == attempts:
                raise
            log.warning("topic %s: attempt %d/%d failed (%s); retrying in %.0fs",
                        req.topic_id, attempt, attempts, exc, settings.generate_retry_delay_s)
            time.sleep(settings.generate_retry_delay_s)
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
        settings: config.Settings | None = None, client: EngineClient | None = None,
        llm=None, day: str | None = None, failed: list[str] | None = None, part: str | None = None) -> list[Path]:
    settings = settings or config.load()
    settings.require("generate", dry_run=dry_run)
    queue = load_queue(settings.queue_path)
    defaults = load_defaults(settings.queue_path)
    state = load_state(settings)
    if topic_id:
        candidates = [*queue, *content_calendar.all_requests(settings, defaults),
                      *story.all_episode_requests(settings, defaults)]
        todo = [r for r in candidates if r.topic_id == topic_id]
        if not todo:
            raise SystemExit(f"topic {topic_id!r} not found in {settings.queue_path}, the content calendar "
                             f"or planned series")
    elif day:
        todo = [r for d, r in content_calendar.episodes(settings, defaults)
                if d == day and (not part or r.topic_id.endswith(f"-{part}"))]
        if not todo:
            raise SystemExit(f"no content-calendar episodes on {day}{f' ({part})' if part else ''} "
                             f"in {settings.calendar_dir}")
        uploaded = {p.stem for p in review.stage_dir(settings.output_dir, review.UPLOADED).glob("*.json")}
        todo = [r for r in todo if r.topic_id not in uploaded and state.get(r.topic_id, {}).get("status") != "generated"]
    else:
        todo = select_topics(settings, queue, defaults, state,
                             count or settings.daily_generate_count, dry_run=dry_run, llm=llm)
    if not todo:
        log.info("nothing to generate (queue done, no unfinished episodes, and no new series planned)")
        return []
    if not dry_run and client is None:
        client = EngineClient(settings)

    produced = []
    for req in todo:
        try:
            mp4 = generate_one(req, settings, client, dry_run=dry_run)
        except RETRIABLE_ERRORS as exc:
            log.error("topic %s failed: %s", req.topic_id, exc)
            if failed is not None:
                failed.append(req.topic_id)
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
    p.add_argument("--count", type=int, help="number of videos to generate (default DAILY_GENERATE_COUNT)")
    p.add_argument("--topic", help="generate one specific topic or episode id")
    p.add_argument("--date", help="generate every episode of this content-calendar day (YYYY-MM-DD)")
    p.add_argument("--part", choices=["all", "p1", "p2", "p3"], default="all", help="with --date: only this part")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)
    failed: list[str] = []
    run(count=args.count, topic_id=args.topic, dry_run=args.dry_run, day=args.date, failed=failed,
        part=None if args.part == "all" else args.part)
    if failed:
        log.error("%d video(s) failed after retries: %s", len(failed), ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
