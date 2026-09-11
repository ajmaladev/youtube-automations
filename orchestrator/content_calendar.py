"""Pre-written monthly content calendar: topics/calendar/YYYY-MM.json.

One researched 3-part series per day: p1 posts in the morning, p2 in the afternoon, p3 at night.
Everything is written and fact-checked ahead of time, so the pipeline only renders and uploads these
episodes; it never invents a topic for a day the calendar covers.

  schema     topics/calendar/calendar.schema.json
  validate   the editorial rules a JSON schema can't express: one series per day, word counts, fixed endings,
             recaps, hook length, specific cliffhangers, one footage term per clip in script order, no
             Marvel/DC content
  footage    `python -m orchestrator.footage storyboard` previews the Pexels clip each term will play
  assemble   merges per-day drafts (topics/calendar/drafts/YYYY-MM/*.json) into the month file
  generate   episodes up to CALENDAR_LOOKAHEAD_DAYS ahead are rendered oldest first (see generate.py)
  upload     with YOUTUBE_SCHEDULE_PUBLISH=true, approved episodes are scheduled for their slot time

Writing a new month: run /plan-month YYYY-MM in Claude Code (.claude/commands/plan-month.md).

CLI: python -m orchestrator.content_calendar validate [FILE_OR_DIR ...]
     python -m orchestrator.content_calendar assemble YYYY-MM [--timezone Europe/London]
     python -m orchestrator.content_calendar titles
     python -m orchestrator.content_calendar show [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import calendar as cal
import json
import logging
import math
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestrator import config
from orchestrator.models import VideoRequest

log = logging.getLogger(__name__)

PARTS = ("p1", "p2", "p3")
SLOTS = {"p1": "morning", "p2": "afternoon", "p3": "night"}
SLOT_TIMES = {"p1": "09:00", "p2": "15:00", "p3": "21:00"}
ENDINGS = {
    "p1": "Subscribe so you don't miss Part 2.",
    "p2": "Subscribe so you don't miss Part 3.",
    "p3": "Subscribe for a new true story every day.",
}
CATEGORIES = ("nature", "history", "science", "adventure")
WORDS = (80, 110)          # whole script including the ending: ~35-45 seconds of narration
MAX_HOOK_WORDS = 12
MAX_SENTENCE_WORDS = 24
MAX_NUMBERS = 4            # digit groups per script, not counting "Part N"
TERMS = (8, 14)            # video_terms per episode
WORDS_PER_CLIP = 8.5       # the voice reads ~8.5 words in one 3-second clip; one term = one clip, in script order
MONTH_FILE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])\.json$")
TOP_KEYS = {"schema_version", "channel", "month", "timezone", "slots", "series"}
SERIES_KEYS = {"series_id", "date", "category", "series_title", "throughline", "sources", "episodes"}
EPISODE_KEYS = {"episode_id", "date", "part", "slot", "series_id", "series_title", "episode_title",
                "youtube_title", "script", "cliffhanger", "video_terms", "description", "tags"}
BANNED_SCRIPT = re.compile(r"(did you know|in this video|hey guys|welcome back|subscribe|follow for|like and share|"
                           r"comment below|smash that|[#*_`\[\]]|[^\x00-\x7f])", re.I)
BANNED_TERMS = re.compile(r"\b(marvel|dc|batman|superman|spider ?man|wonder woman|captain america|hulk|wolverine|"
                          r"deadpool|x men|avengers|godzilla|nemo|disney|pixar|comic|comics|cartoon|anime|logo|"
                          r"illustration|drawing|poster)\b")
BANNED_FRANCHISES = re.compile(r"\b(marvel|dc comics|batman|superman|wonder woman|spider-?man|avengers|"
                               r"captain america|iron man|deadpool|x-men|wolverine|hulk|joker|justice league|"
                               r"black panther|ant-man)\b", re.I)
DATE_OPENER = re.compile(r"^((in|on|by) (early |late )?([a-z]+ )?)?\d", re.I)
GENERIC_CLIFF = re.compile(r"^(so |and |but )?(what happened( next)?|what (would|will|did) (he|she|they|it|the \w+) "
                           r"do( next)?|how did .{1,40} react|who was (he|she|it|this)|what came next)\?$", re.I)


class CalendarError(RuntimeError):
    pass


# --- validation -------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _zone(name: Any) -> ZoneInfo | None:
    """None means the local time of the machine running the scheduler."""
    return None if name == "local" else ZoneInfo(str(name))


def terms_needed(script: str) -> int:
    """Footage terms an episode needs so the clips (one per term) cover the whole narration."""
    return max(TERMS[0], math.ceil(len(script.split()) / WORDS_PER_CLIP))


def _check_episode(ep: dict[str, Any], n: int, series: dict[str, Any], errors: list[str]) -> None:
    part, sid, title = PARTS[n - 1], series.get("series_id"), series.get("series_title")

    def err(msg: str) -> None:
        errors.append(f"{sid}-{part}: {msg}")

    missing, extra = EPISODE_KEYS - set(ep), set(ep) - EPISODE_KEYS
    if missing or extra:
        err(f"keys: missing {sorted(missing)}, unexpected {sorted(extra)}")
    if ep.get("episode_id") != f"{sid}-{part}":
        err("episode_id must be <series_id>-<part>")
    if (ep.get("date"), ep.get("series_id"), ep.get("series_title")) != (series.get("date"), sid, title):
        err("date, series_id and series_title must match the series")
    if ep.get("slot") != SLOTS[part]:
        err(f"slot must be {SLOTS[part]!r}")
    if ep.get("youtube_title") != f"{title} (Part {n}/3)":
        err(f"youtube_title must be {f'{title} (Part {n}/3)'!r}")
    if not 5 <= len(str(ep.get("episode_title", ""))) <= 60:
        err("episode_title must be 5-60 chars")

    script = str(ep.get("script", ""))
    words = len(script.split())
    if not WORDS[0] <= words <= WORDS[1]:
        err(f"script has {words} words ({WORDS[0]}-{WORDS[1]} including the ending)")
    body = script
    if script.endswith(ENDINGS[part]):
        body = script[: -len(ENDINGS[part])].strip()
    else:
        err(f"script must end with {ENDINGS[part]!r}")
    bad = BANNED_SCRIPT.search(body)
    if bad:
        err(f"script contains {bad.group(0)!r} (no CTAs, markdown, emojis or non-ASCII)")
    text = " ".join([*(str(ep.get(k, "")) for k in ("episode_title", "script", "description", "cliffhanger")),
                     *map(str, ep.get("tags") or [])])
    franchise = BANNED_FRANCHISES.search(text)
    if franchise:
        err(f"mentions {franchise.group(0)!r}: the channel doesn't cover Marvel or DC")
    sentences = _sentences(body)
    if not sentences:
        err("script is empty")
        return
    if len(sentences[0].split()) > MAX_HOOK_WORDS:
        err(f"hook is {len(sentences[0].split())} words (max {MAX_HOOK_WORDS}): {sentences[0]!r}")
    if DATE_OPENER.match(sentences[0]):
        err(f"hook must not open with a date: {sentences[0]!r}")
    too_long = [s for s in sentences if len(s.split()) > MAX_SENTENCE_WORDS]
    if too_long:
        err(f"sentence over {MAX_SENTENCE_WORDS} words: {too_long[0][:70]!r}")
    numbers = re.findall(r"\d[\d,.]*", re.sub(r"\bPart \d\b", "", body))
    if len(numbers) > MAX_NUMBERS:
        err(f"{len(numbers)} numbers in the script (max {MAX_NUMBERS}): {numbers}")
    if n > 1 and (len(sentences) < 2 or not sentences[1].startswith(f"In Part {n - 1},")):
        err(f"sentence 2 must be a recap starting 'In Part {n - 1},'")
    cliffhanger = str(ep.get("cliffhanger", ""))
    if part == "p3":
        if cliffhanger:
            err("cliffhanger must be empty for p3")
    elif not cliffhanger.strip():
        err("cliffhanger is empty")
    elif GENERIC_CLIFF.match(sentences[-1]):
        err(f"generic cliffhanger, tease something specific: {sentences[-1]!r}")

    terms = [str(t) for t in ep.get("video_terms") or []]
    need = terms_needed(script)
    if not need <= len(terms) <= TERMS[1]:
        err(f"video_terms has {len(terms)} terms; this script needs {need}-{TERMS[1]} "
            f"(one per ~{WORDS_PER_CLIP:g} words, in script order)")
    bad_terms = [t for t in terms if not re.fullmatch(r"[a-z0-9]+( [a-z0-9]+){1,4}", t) or BANNED_TERMS.search(t)]
    if bad_terms:
        err(f"video_terms must be lowercase 2-5 word filmable scenes, no characters/brands/art: {bad_terms}")
    if len(set(terms)) != len(terms):
        err("video_terms repeat a phrase (each term must get its own clip)")
    desc = str(ep.get("description", ""))
    if not 20 <= len(desc) <= 300 or "http" in desc or "?" not in desc:
        err("description must be 20-300 chars, have no URLs and ask viewers a question")
    tags = ep.get("tags") or []
    if (not 5 <= len(tags) <= 8 or len(set(tags)) != len(tags)
            or any(not re.fullmatch(r"[a-z0-9][a-z0-9 '-]*", str(t)) for t in tags)):
        err(f"tags need 5-8 unique lowercase entries without '#': {tags}")


def _check_series(s: dict[str, Any], errors: list[str]) -> None:
    sid = str(s.get("series_id", "?"))

    def err(msg: str) -> None:
        errors.append(f"{sid}: {msg}")

    missing, extra = SERIES_KEYS - set(s), set(s) - SERIES_KEYS
    if missing or extra:
        err(f"keys: missing {sorted(missing)}, unexpected {sorted(extra)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*", sid) or not sid.startswith(f"{s.get('date')}-"):
        err("series_id must be <date>-<slug>")
    if s.get("category") not in CATEGORIES:
        err(f"category must be one of {CATEGORIES}")
    title = str(s.get("series_title", ""))
    if not 5 <= len(title) <= 45:
        err(f"series_title is {len(title)} chars (5-45)")
    if not str(s.get("throughline", "")).strip():
        err("throughline is empty")
    franchise = BANNED_FRANCHISES.search(f"{title} {s.get('throughline', '')}")
    if franchise:
        err(f"mentions {franchise.group(0)!r}: the channel doesn't cover Marvel or DC")
    sources = s.get("sources") or []
    if len(sources) < 2 or any(not isinstance(x, dict) or set(x) != {"title", "url"}
                               or not str(x["url"]).startswith("https://") for x in sources):
        err("sources need 2+ {title, url} entries with https URLs")
    episodes = s.get("episodes") or []
    if [e.get("part") if isinstance(e, dict) else None for e in episodes] != list(PARTS):
        err("episodes must be p1, p2, p3 in order")
        return
    for n, ep in enumerate(episodes, start=1):
        _check_episode(ep, n, s, errors)


def _check_month(doc: dict[str, Any], errors: list[str]) -> None:
    missing, extra = TOP_KEYS - set(doc), set(doc) - TOP_KEYS - {"$schema"}
    if missing or extra:
        errors.append(f"top-level keys: missing {sorted(missing)}, unexpected {sorted(extra)}")
    if doc.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    try:
        _zone(doc.get("timezone"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        errors.append(f"timezone {doc.get('timezone')!r} is not 'local' or an IANA zone")
    slots = doc.get("slots") or {}
    for part in PARTS:
        slot = slots.get(part) or {}
        if slot.get("label") != SLOTS[part] or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(slot.get("time"))):
            errors.append(f"slots.{part} must be {{label: {SLOTS[part]!r}, time: 'HH:MM'}}")
    series = doc.get("series") or []
    month = str(doc.get("month", ""))
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        y, m = map(int, month.split("-"))
        want = [date(y, m, d).isoformat() for d in range(1, cal.monthrange(y, m)[1] + 1)]
        got = [s.get("date") for s in series]
        if got != want:
            errors.append(f"series must cover every day of {month} once, in order "
                          f"(missing {sorted(set(want) - set(got))}, extra {sorted(set(got) - set(want))})")
    else:
        errors.append("month must be YYYY-MM")
    for field in ("series_id", "series_title"):
        values = [str(s.get(field, "")).lower() for s in series]
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            errors.append(f"duplicate {field}: {dupes}")
    for s in series:
        _check_series(s, errors)


def validate_file(path: Path) -> list[str]:
    """Errors for a month file, or for a single-series draft (an object with series_id)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cannot read {path}: {exc}"]
    errors: list[str] = []
    if not isinstance(doc, dict):
        errors.append("top level must be a JSON object")
    elif "series_id" in doc:
        _check_series(doc, errors)
    else:
        _check_month(doc, errors)
    return errors


# --- loading ------------------------------------------------------------------------------

def month_files(directory: Path) -> list[Path]:
    return sorted(p for p in Path(directory).glob("*.json") if MONTH_FILE.match(p.name))


def drafts_dir(settings: config.Settings, month: str) -> Path:
    return settings.calendar_dir / "drafts" / month


def publish_at(doc: dict[str, Any], episode: dict[str, Any]) -> str:
    """The episode's slot as an RFC 3339 UTC timestamp (what YouTube's status.publishAt expects)."""
    hour, minute = map(int, doc["slots"][episode["part"]]["time"].split(":"))
    y, m, d = map(int, episode["date"].split("-"))
    zone = _zone(doc.get("timezone", "local"))
    local = datetime(y, m, d, hour, minute, tzinfo=zone) if zone else datetime(y, m, d, hour, minute).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def episode_request(doc: dict[str, Any], series: dict[str, Any], episode: dict[str, Any],
                    defaults: dict[str, Any], channel: dict[str, Any]) -> VideoRequest:
    n = PARTS.index(episode["part"]) + 1
    base = {k: v for k, v in defaults.items() if k not in {"video_script_prompt", "title", "description"}}
    sources = "\n".join(s["url"] for s in series["sources"][:3])
    description = (f"{episode['description']}\n\n"
                   f"Part {n} of 3: {series['series_title']}\n\n"
                   f"Sources:\n{sources}\n\n"
                   f"{channel['description_footer']}\n\n"
                   f"#shorts #truestory").strip()
    tags = list(dict.fromkeys([*base.get("tags", []), *episode["tags"], series["category"]]))
    return VideoRequest.from_dict({
        **base,
        "topic_id": episode["episode_id"],
        "subject": f"{series['series_title']} - part {n}: {episode['episode_title']}",
        "title": episode["youtube_title"],
        "description": description,
        "tags": tags,
        "publish_at": publish_at(doc, episode),
        "video_script": episode["script"],
        "video_terms": ", ".join(episode["video_terms"]),
    })


def episodes(settings: config.Settings, defaults: dict[str, Any]) -> list[tuple[str, VideoRequest]]:
    """(date, request) for every calendar episode, in posting order."""
    from orchestrator.story import load_channel  # story pulls in the LLM client; only needed here

    channel = load_channel(settings.channel_path)
    out = []
    for path in month_files(settings.calendar_dir):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            for series in doc["series"]:
                for ep in series["episodes"]:
                    out.append((ep["date"], episode_request(doc, series, ep, defaults, channel)))
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarError(f"{path}: malformed calendar ({exc!r}); run "
                                f"`python -m orchestrator.content_calendar validate {path}`") from exc
    return sorted(out, key=lambda item: (item[0], item[1].topic_id[-2:]))


def all_requests(settings: config.Settings, defaults: dict[str, Any]) -> list[VideoRequest]:
    return [req for _, req in episodes(settings, defaults)]


def due_episodes(settings: config.Settings, state: dict[str, Any], defaults: dict[str, Any],
                 today: date | None = None) -> list[VideoRequest]:
    """Unrendered episodes dated up to CALENDAR_LOOKAHEAD_DAYS ahead, oldest first."""
    last = ((today or date.today()) + timedelta(days=settings.calendar_lookahead_days)).isoformat()
    return [req for day, req in episodes(settings, defaults)
            if day <= last and state.get(req.topic_id, {}).get("status") != "generated"]


def covers(settings: config.Settings, day: date | None = None) -> bool:
    """True if a calendar file has a series for `day` (then no series is invented for it)."""
    wanted = (day or date.today()).isoformat()
    for path in month_files(settings.calendar_dir):
        try:
            series = json.loads(path.read_text(encoding="utf-8")).get("series") or []
        except (OSError, ValueError, AttributeError):
            continue
        if any(isinstance(s, dict) and s.get("date") == wanted for s in series):
            return True
    return False


# --- writing a month ----------------------------------------------------------------------

def assemble(settings: config.Settings, month: str, timezone_name: str = "Europe/London") -> tuple[Path, list[str]]:
    """Merge topics/calendar/drafts/<month>/*.json into topics/calendar/<month>.json; returns (path, errors)."""
    from orchestrator.story import load_channel

    series = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(drafts_dir(settings, month).glob("*.json"))]
    doc = {
        "$schema": "./calendar.schema.json",
        "schema_version": 1,
        "channel": load_channel(settings.channel_path)["channel_name"],
        "month": month,
        "timezone": timezone_name,
        "slots": {part: {"label": SLOTS[part], "time": SLOT_TIMES[part]} for part in PARTS},
        "series": sorted(series, key=lambda s: str(s.get("date", ""))),
    }
    out = settings.calendar_dir / f"{month}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out, validate_file(out)


def past_series(settings: config.Settings) -> list[dict[str, str]]:
    """Every series already planned (month files and drafts), so a new month never repeats a story."""
    seen: dict[str, dict[str, str]] = {}
    files = [*month_files(settings.calendar_dir), *sorted((settings.calendar_dir / "drafts").glob("*/*.json"))]
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in [doc] if "series_id" in doc else doc.get("series") or []:
            seen[str(s.get("series_id"))] = {k: str(s.get(k, "")) for k in ("date", "category", "series_title",
                                                                           "throughline")}
    return sorted(seen.values(), key=lambda s: s["date"])


def _expand(paths: list[Path]) -> list[Path]:
    return [f for p in paths for f in (sorted(p.glob("*.json")) if p.is_dir() else [p])
            if not f.name.endswith(".schema.json")]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.content_calendar", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["validate", "assemble", "titles", "show"])
    p.add_argument("args", nargs="*", help="validate: files or draft folders (default: every month file); "
                                           "assemble: the month, YYYY-MM")
    p.add_argument("--date", help="show: YYYY-MM-DD (default today)")
    p.add_argument("--timezone", default="Europe/London", help="assemble: IANA zone for the slot times")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = config.load()

    if args.action == "validate":
        paths = _expand([Path(a) for a in args.args]) or month_files(settings.calendar_dir)
        if not paths:
            print(f"no calendar files in {settings.calendar_dir}")
            return 1
        failed = 0
        for path in paths:
            errors = validate_file(path)
            print(f"{path}: {'OK' if not errors else f'{len(errors)} error(s)'}")
            for e in errors:
                print(f"  - {e}")
            failed += bool(errors)
        return 1 if failed else 0

    if args.action == "assemble":
        if len(args.args) != 1 or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.args[0]):
            p.error("assemble needs the month, e.g. assemble 2026-11")
        out, errors = assemble(settings, args.args[0], args.timezone)
        print(f"wrote {out}: {'OK' if not errors else f'{len(errors)} error(s)'}")
        for e in errors:
            print(f"  - {e}")
        return 1 if errors else 0

    if args.action == "titles":
        for s in past_series(settings):
            print(f"{s['date']}  {s['category']:<10} {s['series_title']}  |  {s['throughline']}")
        return 0

    from orchestrator.generate import load_defaults
    day = args.date or date.today().isoformat()
    found = [req for d, req in episodes(settings, load_defaults(settings.queue_path)) if d == day]
    for req in found:
        script = req.extra_params["video_script"]
        print(f"{req.topic_id}  (publish {req.publish_at}, {len(script.split())} words)\n  {req.title}\n  {script}\n")
    if not found:
        print(f"no calendar episodes on {day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
