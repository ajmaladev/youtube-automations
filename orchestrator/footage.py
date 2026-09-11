"""Preview the stock footage the video engine will pick for every sentence, before anything is rendered.

With match_materials_to_script (topics/queue.yaml) the engine searches Pexels once per video term (portrait,
20 results), keeps clips that are at least one clip long and have an exact 1080x1920 file, and plays each
term's first unused result in script order, one clip per term. This tool runs the same search and prints the
title of that clip next to the words it plays under, so you can check the picture matches the narration.

  search       every usable result for a term (best first) - use it to find terms that show the right scene
  cached       terms already searched whose term or clip titles contain your keywords (free, no API call)
  warm         search every term in the given files that isn't cached yet, pacing itself to the hourly
               limit and waiting out throttling, so the storyboard can then be fully checked
  storyboard   the clip each term will actually get, per episode; exits 1 if a term has no clip or isn't
               checked yet

Pexels allows 200 searches an hour on a free key (ask Pexels for unlimited: pexels.com/api). Every search is
cached in output/footage_cache.json for 14 days and saved immediately, and searches from all processes are
spaced at least 2 seconds apart. Once Pexels throttles, search/storyboard show cached results only and mark
the rest NOT CHECKED; `warm` is the command that waits and finishes the job.

CLI: python -m orchestrator.footage search "sled dogs running snow" ["another term" ...]
     python -m orchestrator.footage cached shark beach
     python -m orchestrator.footage warm FILE_OR_DIR [...] [--date YYYY-MM-DD] [--per-hour 190]
     python -m orchestrator.footage storyboard FILE_OR_DIR [...] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from orchestrator import config

API_URL = "https://api.pexels.com/v1/videos/search"
PER_PAGE = 20
FRAME_SIZE = (1080, 1920)
CACHE_DAYS = 14
MIN_CHOICES = 3  # fewer usable results than this is fragile: Pexels results shift over time
MIN_INTERVAL_S = 2.0  # shared spacing between Pexels searches across processes
RETRY_WAITS = (10,)  # one short retry after HTTP 429, then treat the hourly limit as reached
THROTTLE_PAUSE_S = 300  # warm: how long to wait before trying again once Pexels throttles
STALE_LOCK_S = 30
THROTTLED_HINT = ("Pexels allows 200 searches an hour on a free key; cached results are kept. Run "
                  "`python -m orchestrator.footage warm <files>` to finish the preview (or ask Pexels for "
                  "unlimited requests at pexels.com/api)")


class FootageError(RuntimeError):
    pass


class Throttled(FootageError):
    pass


@dataclass
class Clip:
    id: str
    title: str
    url: str
    duration: int


def title_from_url(url: str) -> str:
    """https://www.pexels.com/video/sled-dogs-racing-across-snow-38953031/ -> 'sled dogs racing across snow'"""
    slug = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"-?\d+$", "", slug).replace("-", " ").strip()


def usable_clips(response: dict[str, Any], clip_seconds: int) -> list[Clip]:
    """Results the engine can use, in Pexels order (mirrors the engine's search_videos_pexels filter)."""
    clips = []
    for video in response.get("videos") or []:
        duration = int(video.get("duration") or 0)
        if duration < clip_seconds:
            continue
        sizes = {(int(f.get("width") or 0), int(f.get("height") or 0)) for f in video.get("video_files") or []}
        if FRAME_SIZE not in sizes:
            continue
        clips.append(Clip(str(video.get("id")), title_from_url(video.get("url", "")), str(video.get("url", "")),
                          duration))
    return clips


class Footage:
    def __init__(self, api_key: str, cache_path: Path, clip_seconds: int = 3, session: Any = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key
        self.cache_path = Path(cache_path)
        self.pace_path = self.cache_path.with_name(self.cache_path.name + ".pace")
        self.clip_seconds = clip_seconds
        self.session = session or requests.Session()
        self.now = now
        self.sleep = sleep
        self.throttled = False
        self.cache: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _key(self, term: str) -> str:
        return f"{term.strip().lower()}|{self.clip_seconds}"

    def _wait_turn(self) -> None:
        """Keep Pexels searches MIN_INTERVAL_S apart across every process that shares the cache folder."""
        lock = self.pace_path.with_name(self.pace_path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3000):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > STALE_LOCK_S:  # left behind by a crashed run
                        lock.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(0.05)
        else:
            raise FootageError(f"could not get the Pexels pacing lock {lock}")
        try:
            try:
                last = float(self.pace_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                last = 0.0
            delay = last + MIN_INTERVAL_S - self.now()
            if 0 < delay <= MIN_INTERVAL_S:
                self.sleep(delay)
            self.pace_path.write_text(str(self.now()), encoding="utf-8")
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def cached(self, term: str) -> list[Clip] | None:
        hit = self.cache.get(self._key(term))
        if hit and self.now() - hit["at"] < CACHE_DAYS * 86400:
            return [Clip(**c) for c in hit["clips"]]
        return None

    def search(self, term: str) -> list[Clip]:
        hit = self.cached(term)
        if hit is not None:
            return hit
        if self.throttled:
            raise Throttled(THROTTLED_HINT)
        for wait in (*RETRY_WAITS, None):
            self._wait_turn()
            resp = self.session.get(API_URL, params={"query": term, "per_page": PER_PAGE, "orientation": "portrait"},
                                    headers={"Authorization": self.api_key}, timeout=30)
            if resp.status_code != 429 or wait is None:
                break
            self.sleep(wait)
        if resp.status_code == 429:
            self.throttled = True
            raise Throttled(THROTTLED_HINT)
        if resp.status_code != 200:
            raise FootageError(f"Pexels search for {term!r} failed: HTTP {resp.status_code}")
        clips = usable_clips(resp.json(), self.clip_seconds)
        self.cache[self._key(term)] = {"at": self.now(), "clips": [asdict(c) for c in clips]}
        self.save()  # keep every paid-for search, even if this run is interrupted
        return clips

    def save(self) -> None:
        """Merge into the cache on disk (parallel runs may have added entries), then swap the file in atomically."""
        merged = {**self._load(), **self.cache}
        self.cache = merged
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_name(f"{self.cache_path.name}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
        for _ in range(5):
            try:
                tmp.replace(self.cache_path)
                return
            except PermissionError:  # Windows: another run is swapping the file in right now
                time.sleep(0.2)
        tmp.unlink(missing_ok=True)

    def find_cached(self, keywords: list[str]) -> list[tuple[str, list[Clip]]]:
        """Cached terms whose term or top clip titles contain every keyword."""
        words = [k.lower() for k in keywords]
        out = []
        for key, entry in sorted(self._load().items()):
            term = key.rsplit("|", 1)[0]
            clips = [Clip(**c) for c in entry.get("clips", [])]
            text = " ".join([term, *(c.title for c in clips[:3])]).lower()
            if all(w in text for w in words):
                out.append((term, clips))
        return out

    def warm(self, terms: list[str], per_hour: int = 190, log: Callable[[str], None] = print) -> int:
        """Search every uncached term, spaced to stay under `per_hour` and waiting out throttling. Returns count."""
        todo = list(dict.fromkeys(t for t in terms if self.cached(t) is None))
        interval = 3600 / max(1, per_hour)
        log(f"{len(todo)} term(s) to preview at up to {per_hour} an hour "
            f"(about {math.ceil(len(todo) * interval / 60)} min)")
        for i, term in enumerate(todo, 1):
            while True:
                try:
                    clips = self.search(term)
                    break
                except Throttled:
                    self.throttled = False
                    log(f"Pexels is throttling; waiting {THROTTLE_PAUSE_S // 60} min ({i - 1}/{len(todo)} done)")
                    self.sleep(THROTTLE_PAUSE_S)
            log(f"{i}/{len(todo)} {term!r}: {len(clips)} usable")
            if i < len(todo):
                self.sleep(interval)
        return len(todo)


def storyboard(footage: Footage, terms: list[str]) -> list[tuple[str, Clip | None, int | None]]:
    """(term, clip the engine will play, usable results or None if not checked) in script order.

    A clip is used only once, like the engine. After Pexels throttles, uncached terms come back unchecked."""
    used: set[str] = set()
    rows: list[tuple[str, Clip | None, int | None]] = []
    for term in terms:
        try:
            clips = footage.search(term)
        except Throttled:
            rows.append((term, None, None))
            continue
        pick = next((c for c in clips if c.id not in used), None)
        if pick:
            used.add(pick.id)
        rows.append((term, pick, len(clips)))
    return rows


def _episodes(paths: list[Path], day: str | None) -> list[dict[str, Any]]:
    files = [f for p in paths for f in (sorted(p.glob("*.json")) if p.is_dir() else [p])]
    out = []
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        for series in [doc] if "series_id" in doc else doc.get("series") or []:
            if day and series.get("date") != day:
                continue
            out.extend(series.get("episodes") or [])
    return out


def script_chunks(script: str, parts: int) -> list[str]:
    """The narration split evenly into `parts` pieces: roughly the words each clip plays under."""
    words = script.split()
    parts = max(1, parts)
    bounds = [round(i * len(words) / parts) for i in range(parts + 1)]
    return [" ".join(words[bounds[i]:bounds[i + 1]]) for i in range(parts)]


def main(argv: list[str] | None = None, session: Any = None, sleep: Callable[[float], None] = time.sleep) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.footage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    search_p = sub.add_parser("search", help="list usable Pexels results for terms")
    search_p.add_argument("terms", nargs="+")
    cached_p = sub.add_parser("cached", help="find already-searched terms by keyword (no API calls)")
    cached_p.add_argument("keywords", nargs="+")
    warm_p = sub.add_parser("warm", help="search every uncached term in the files, waiting out the hourly limit")
    warm_p.add_argument("paths", nargs="+", type=Path, help="series drafts, draft folders or month files")
    warm_p.add_argument("--date", help="only this calendar day")
    warm_p.add_argument("--per-hour", type=int, default=190, help="searches per hour to stay under (default 190)")
    board_p = sub.add_parser("storyboard", help="the clip each video term will get, per episode")
    board_p.add_argument("paths", nargs="+", type=Path, help="series drafts, draft folders or month files")
    board_p.add_argument("--date", help="only this calendar day")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    settings = config.load()
    from orchestrator.generate import load_defaults  # generate pulls in the engine client; only needed here

    clip_seconds = int(load_defaults(settings.queue_path).get("video_clip_duration") or 3)
    footage = Footage(settings.pexels_api_key, settings.output_dir / "footage_cache.json", clip_seconds, session,
                      sleep=sleep)
    if args.action == "cached":
        found = footage.find_cached(args.keywords)
        for term, clips in found:
            print(f"{term!r}: {len(clips)} usable -> " + " | ".join(c.title for c in clips[:3]))
        print(f"\n{len(found)} cached term(s)")
        return 0
    if not settings.pexels_api_key:
        print("PEXELS_API_KEY is not set (.env)")
        return 2

    problems = unchecked = checked = 0
    try:
        if args.action == "warm":
            terms = [str(t) for ep in _episodes(args.paths, args.date) for t in ep.get("video_terms") or []]
            footage.warm(terms, args.per_hour)
            print("preview complete: every term is cached; run `storyboard` to review the pairings")
            return 0
        if args.action == "search":
            for term in args.terms:
                try:
                    clips = footage.search(term)
                except Throttled:
                    unchecked += 1
                    print(f"{term!r}: NOT CHECKED (Pexels hourly limit)")
                    continue
                flag = "" if len(clips) >= MIN_CHOICES else "  <- WEAK"
                print(f"{term!r}: {len(clips)} usable{flag}")
                for clip in clips[:8]:
                    print(f"    {clip.title}  ({clip.duration}s)")
                problems += len(clips) < MIN_CHOICES
        else:
            for ep in _episodes(args.paths, args.date):
                terms = [str(t) for t in ep.get("video_terms") or []]
                rows = storyboard(footage, terms)
                print(f"\n{ep.get('episode_id')}  ({len(terms)} terms, {len(str(ep.get('script', '')).split())} words)")
                for i, ((term, clip, count), words) in enumerate(zip(rows, script_chunks(ep.get("script", ""), len(rows))), 1):
                    if count is None:
                        unchecked += 1
                        shown = "NOT CHECKED (Pexels hourly limit) - run `footage warm` on this file"
                    elif clip is None:
                        checked += 1
                        problems += 1
                        shown = "NO CLIP - the timeline shifts; change this term"
                    else:
                        checked += 1
                        shown = clip.title + ("  <- WEAK (few results)" if count < MIN_CHOICES else "")
                        problems += count < MIN_CHOICES
                    print(f"  {i:>2}. \"{words}\"\n      {term}  ->  {shown}")
    except FootageError as exc:
        print(f"error: {exc}")
        return 2
    finally:
        footage.save()
    print(f"\n{checked} checked, {problems} problem term(s), {unchecked} not checked yet")
    if unchecked:
        print(THROTTLED_HINT)
    return 1 if problems or unchecked else 0


if __name__ == "__main__":
    raise SystemExit(main())
