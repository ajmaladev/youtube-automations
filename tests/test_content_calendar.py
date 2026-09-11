"""Content calendar: editorial validation, loading, scheduling and pipeline integration."""
import json
from datetime import date, datetime, timezone

import pytest

from orchestrator import config, generate, story, upload
from orchestrator import content_calendar as cc
from orchestrator.config import REPO_ROOT
from orchestrator.models import VideoRequest

FILLER = "The reef went quiet that night."  # 6 words, no numbers
TERMS = ["coral reef aerial", "fish swimming close up", "ocean waves slow motion", "sunlight underwater",
         "sea anemone swaying", "tropical fish school", "diver swimming over reef", "sea turtle swimming",
         "jellyfish glowing dark", "boat on calm ocean", "sunset over the sea", "waves on sandy beach"]


def make_script(part):
    n = cc.PARTS.index(part) + 1
    recap = [f"In Part {n - 1}, a small fish made a choice."] if n > 1 else []
    return " ".join(["A small fish changed the whole reef.", *recap, *[FILLER] * 13, cc.ENDINGS[part]])


def make_series(day):
    sid, title = f"{day}-reef", f"The Reef Story {day}"
    episodes = [{
        "episode_id": f"{sid}-{part}", "date": day, "part": part, "slot": cc.SLOTS[part],
        "series_id": sid, "series_title": title, "episode_title": f"Chapter {n} of the reef",
        "youtube_title": f"{title} (Part {n}/3)", "script": make_script(part),
        "cliffhanger": "" if part == "p3" else FILLER,
        "video_terms": list(TERMS),
        "description": "A tiny fish with a big secret. Would you have guessed it?",
        "tags": ["clownfish", "ocean", "reef", "true story", "nature"],
    } for n, part in enumerate(cc.PARTS, start=1)]
    return {"series_id": sid, "date": day, "category": "nature", "series_title": title,
            "throughline": "What did the fish do?",
            "sources": [{"title": "A", "url": "https://example.org/a"}, {"title": "B", "url": "https://example.org/b"}],
            "episodes": episodes}


def make_month(days, month="2026-10", tz="UTC"):
    return {"$schema": "./calendar.schema.json", "schema_version": 1, "channel": "Plot Armor Facts",
            "month": month, "timezone": tz,
            "slots": {"p1": {"label": "morning", "time": "09:00"}, "p2": {"label": "afternoon", "time": "15:00"},
                      "p3": {"label": "night", "time": "21:00"}},
            "series": [make_series(d) for d in days]}


def october():
    return [date(2026, 10, d).isoformat() for d in range(1, 32)]


def write(directory, name, doc):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# --- validation -------------------------------------------------------------------------

def test_repo_calendar_files_pass_validation():
    paths = cc.month_files(REPO_ROOT / "topics" / "calendar")
    assert paths, "no calendar months in topics/calendar"
    for path in paths:
        assert cc.validate_file(path) == [], path.name


def test_valid_month_and_single_series_draft_pass(tmp_path):
    assert cc.validate_file(write(tmp_path, "2026-10.json", make_month(october()))) == []
    assert cc.validate_file(write(tmp_path, "draft.json", make_series("2026-10-05"))) == []


def test_validator_reports_editorial_problems(tmp_path):
    doc = make_month(october()[:-1])
    p1, p2, p3 = doc["series"][0]["episodes"]
    p1["script"] = p1["script"].replace(cc.ENDINGS["p1"], "Follow for more.")
    p2["script"] = p2["script"].replace("In Part 1,", "Earlier,")
    p3["script"] = " ".join([FILLER] * 20 + [cc.ENDINGS["p3"]])
    p3["youtube_title"] = "Wrong"
    doc["series"][1]["episodes"][0]["script"] = make_script("p1").replace(
        f"{FILLER} {cc.ENDINGS['p1']}", f"What happened next? {cc.ENDINGS['p1']}")
    doc["series"][2]["episodes"][0]["video_terms"][0] = "batman at night"
    errors = "\n".join(cc.validate_file(write(tmp_path, "2026-10.json", doc)))
    for expected in ["missing ['2026-10-31']", "must end with", "sentence 2 must be a recap",
                     "script has 128 words", "youtube_title must be", "generic cliffhanger", "filmable scenes"]:
        assert expected in errors


def test_every_clip_of_narration_needs_its_own_footage_term(tmp_path):
    series = make_series("2026-10-05")
    series["episodes"][0]["video_terms"] = TERMS[:6]
    series["episodes"][1]["video_terms"] = [*TERMS[:10], TERMS[0]]
    errors = "\n".join(cc.validate_file(write(tmp_path, "draft.json", series)))
    assert "video_terms has 6 terms; this script needs 11-14" in errors
    assert "repeat a phrase" in errors


def test_marvel_and_dc_content_is_rejected(tmp_path):
    series = make_series("2026-10-05")
    series["series_title"] = "How Superman Was Sold"
    series["episodes"][1]["description"] = "The real story behind Spider-Man. Did you know?"
    errors = "\n".join(cc.validate_file(write(tmp_path, "draft.json", series)))
    assert "'Superman'" in errors and "'Spider-Man'" in errors


def test_cli_validate_exit_codes(tmp_path, capsys):
    good = write(tmp_path, "2026-10.json", make_month(october()))
    bad = write(tmp_path / "bad", "2026-11.json", make_month(["2026-11-01"], month="2026-11"))
    assert cc.main(["validate", str(good)]) == 0
    assert cc.main(["validate", str(bad.parent)]) == 1
    assert "error(s)" in capsys.readouterr().out


def test_assemble_merges_drafts_and_titles_lists_every_planned_series(settings, capsys):
    drafts = cc.drafts_dir(settings, "2026-10")
    for day in reversed(october()):
        write(drafts, f"{day}.json", make_series(day))
    assert cc.main(["assemble", "2026-10"]) == 0
    doc = json.loads((settings.calendar_dir / "2026-10.json").read_text(encoding="utf-8"))
    assert [s["date"] for s in doc["series"]] == october() and doc["timezone"] == "Europe/London"
    titles = cc.past_series(settings)
    assert len(titles) == 31 and titles[0]["series_title"] == "The Reef Story 2026-10-01"
    capsys.readouterr()
    assert cc.main(["titles"]) == 0 and "The Reef Story 2026-10-31" in capsys.readouterr().out


# --- loading ------------------------------------------------------------------------------

def test_publish_at_converts_slot_to_utc():
    doc = make_month(["2026-10-01"], tz="Asia/Kolkata")
    assert cc.publish_at(doc, doc["series"][0]["episodes"][0]) == "2026-10-01T03:30:00Z"


def test_due_episodes_respect_lookahead_state_and_order(settings):
    days = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    write(settings.calendar_dir, "2026-10.json", make_month(days))
    state = {"2026-10-01-reef-p1": {"status": "generated"}, "2026-10-01-reef-p2": {"status": "failed"}}
    due = cc.due_episodes(settings, state, {}, today=date(2026, 10, 2))
    assert [r.topic_id for r in due] == [f"{d}-reef-{p}" for d in days[:3] for p in cc.PARTS][1:]


def test_episode_request_is_ready_to_render_and_upload(settings):
    write(settings.calendar_dir, "2026-10.json", make_month(["2026-10-01"]))
    defaults = {"tags": ["shorts"], "voice_name": "v", "video_script_prompt": "ignored",
                "match_materials_to_script": True}
    req = cc.all_requests(settings, defaults)[1]
    assert req.topic_id == "2026-10-01-reef-p2" and req.title == "The Reef Story 2026-10-01 (Part 2/3)"
    assert "Part 2 of 3: The Reef Story 2026-10-01" in req.description
    assert "https://example.org/a" in req.description and req.description.endswith("#shorts #truestory")
    assert req.tags == ["shorts", "clownfish", "ocean", "reef", "true story", "nature"]
    assert req.publish_at == "2026-10-01T15:00:00Z"
    payload = req.to_engine_payload()
    assert payload["video_script"].endswith(cc.ENDINGS["p2"])
    assert payload["video_terms"].startswith("coral reef aerial, ") and payload["voice_name"] == "v"
    assert payload["match_materials_to_script"] is True
    assert "publish_at" not in payload and "video_script_prompt" not in payload


def test_queue_defaults_play_clips_in_script_order():
    defaults = generate.load_defaults(REPO_ROOT / "topics" / "queue.yaml")
    assert defaults["match_materials_to_script"] is True and defaults["video_concat_mode"] == "sequential"


# --- pipeline -----------------------------------------------------------------------------

def test_calendar_comes_before_series_and_its_days_are_never_invented(settings, monkeypatch):
    today = date.today().isoformat()
    write(settings.calendar_dir, "2026-10.json", make_month([today]))
    monkeypatch.setenv("AUTO_SERIES", "true")
    monkeypatch.setattr(story, "plan_and_save", lambda *a, **k: pytest.fail("planned a series for a calendar day"))
    todo = generate.select_topics(config.load(), [], {}, {f"{today}-reef-p1": {"status": "generated"}}, count=3)
    assert [r.topic_id for r in todo] == [f"{today}-reef-p2", f"{today}-reef-p3"]


def test_generate_topic_finds_calendar_episode(settings, caplog):
    caplog.set_level("INFO")
    write(settings.calendar_dir, "2026-10.json", make_month(["2026-10-01"]))
    assert generate.run(topic_id="2026-10-01-reef-p3", dry_run=True, settings=settings) == []
    assert cc.ENDINGS["p3"] in caplog.text


def test_upload_schedules_slot_only_when_enabled_private_and_in_future():
    now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    req = VideoRequest(topic_id="e", subject="s", publish_at="2026-10-01T09:00:00Z")
    assert "publishAt" not in upload.build_body(req, now=now)["status"]
    assert upload.build_body(req, schedule_publish=True, now=now)["status"]["publishAt"] == "2026-10-01T09:00:00Z"
    late = datetime(2026, 10, 1, 8, 50, tzinfo=timezone.utc)
    assert upload.build_body(req, schedule_publish=True, now=late)["status"]["publishAt"] == "2026-10-01T09:10:00Z"
    unlisted = VideoRequest(topic_id="e", subject="s", publish_at="2026-10-01T09:00:00Z", privacy_status="unlisted")
    assert "publishAt" not in upload.build_body(unlisted, schedule_publish=True, now=now)["status"]
