import json

import pytest

from orchestrator import footage


def video(vid, slug, duration=10, sizes=((1080, 1920), (720, 1280))):
    return {"id": vid, "duration": duration, "url": f"https://www.pexels.com/video/{slug}-{vid}/",
            "video_files": [{"width": w, "height": h, "link": f"https://videos.pexels.com/{vid}-{w}.mp4"}
                            for w, h in sizes]}


RESULTS = {
    "sled dogs": [video(1, "sled-dogs-racing-across-snow"), video(2, "husky-team-pulling-sled"),
                  video(3, "short-clip", duration=2), video(4, "landscape-only", sizes=((1920, 1080),)),
                  video(7, "dog-sled-team-at-sunrise")],
    "husky": [video(1, "sled-dogs-racing-across-snow"), video(5, "husky-close-up-in-snow"),
              video(6, "husky-puppy")],
    "nothing": [],
}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Session:
    def __init__(self, status=200, throttle_after=None):
        self.calls, self.status, self.throttle_after = [], status, throttle_after

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params["query"])
        if self.throttle_after is not None and len(self.calls) > self.throttle_after:
            return Resp(429, {})
        return Resp(self.status, {"videos": RESULTS.get(params["query"], [])})


def make(tmp_path, session=None, **kw):
    kw.setdefault("sleep", lambda s: None)
    return footage.Footage("k", tmp_path / "cache.json", session=session or Session(), **kw)


def test_title_comes_from_the_pexels_page_slug():
    assert footage.title_from_url("https://www.pexels.com/video/sled-dogs-racing-38953031/") == "sled dogs racing"


def test_only_clips_the_engine_can_use_are_counted(tmp_path):
    assert [c.title for c in make(tmp_path).search("sled dogs")] == [
        "sled dogs racing across snow", "husky team pulling sled", "dog sled team at sunrise"]


def test_storyboard_never_reuses_a_clip_and_flags_missing_footage(tmp_path):
    rows = footage.storyboard(make(tmp_path), ["sled dogs", "husky", "nothing"])
    assert [(t, c.title if c else None, n) for t, c, n in rows] == [
        ("sled dogs", "sled dogs racing across snow", 3),
        ("husky", "husky close up in snow", 3),
        ("nothing", None, 0),
    ]


def test_every_search_is_saved_at_once_and_shared_between_runs(tmp_path):
    session = Session()
    first, second = make(tmp_path, session), make(tmp_path, session)
    first.search("husky")
    second.search("sled dogs")  # saving must keep the entry the first run already saved
    third = make(tmp_path, session)
    assert len(third.search("HUSKY ")) == 3 and len(third.search("sled dogs")) == 3
    assert session.calls == ["husky", "sled dogs"]


def test_a_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "cache.json").write_text("{not json")
    assert len(make(tmp_path).search("husky")) == 3


def test_searches_share_one_pace_across_runs(tmp_path):
    clock = iter([100.0, 100.0, 100.5, 100.5, 100.5])  # second run searches half a second after the first
    waits = []
    session = Session()
    make(tmp_path, session, now=lambda: 100.0, sleep=waits.append).search("husky")
    make(tmp_path, session, now=lambda: next(clock), sleep=waits.append).search("sled dogs")
    assert waits and waits[0] == pytest.approx(footage.MIN_INTERVAL_S)


def test_once_throttled_the_storyboard_marks_uncached_terms_unchecked_without_more_calls(tmp_path):
    session = Session(throttle_after=1)
    f = make(tmp_path, session)
    rows = footage.storyboard(f, ["husky", "sled dogs", "nothing"])
    assert [(t, n) for t, _, n in rows] == [("husky", 3), ("sled dogs", None), ("nothing", None)]
    assert session.calls == ["husky", "sled dogs", "sled dogs"]  # one short retry, then no more calls


def test_cached_finds_known_terms_by_keyword_without_calling_pexels(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    footage.Footage("k", tmp_path / "footage_cache.json", session=Session(), sleep=lambda s: None).search("sled dogs")
    assert footage.main(["cached", "snow"], session=Session(status=500)) == 0
    assert "'sled dogs': 3 usable -> sled dogs racing across snow" in capsys.readouterr().out


def test_warm_waits_out_throttling_until_every_term_is_cached(tmp_path):
    session, waits, logs = Session(throttle_after=1), [], []
    f = make(tmp_path, session, sleep=waits.append)

    def unthrottle(seconds):
        waits.append(seconds)
        if seconds == footage.THROTTLE_PAUSE_S:
            session.throttle_after = None  # the hourly window has rolled over

    f.sleep = unthrottle
    assert f.warm(["husky", "sled dogs", "husky"], per_hour=360, log=logs.append) == 2
    assert f.cached("husky") is not None and f.cached("sled dogs") is not None
    assert footage.THROTTLE_PAUSE_S in waits and 10.0 in waits  # paced at 3600/360 seconds, waited once
    assert any("throttling" in line for line in logs)
    assert f.warm(["husky", "sled dogs"], log=logs.append) == 0  # nothing left to search


def test_script_chunks_cover_every_word_evenly():
    chunks = footage.script_chunks(" ".join(str(i) for i in range(99)), 12)
    assert len(chunks) == 12 and all(chunks) and " ".join(chunks).split() == [str(i) for i in range(99)]


def test_storyboard_cli_fails_when_a_term_has_no_clip(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    draft = tmp_path / "2026-11-01.json"
    ep = {"episode_id": "2026-11-01-x-p1", "script": "one two three four five six", "video_terms": ["sled dogs", "husky"]}
    draft.write_text(json.dumps({"series_id": "2026-11-01-x", "date": "2026-11-01", "episodes": [ep]}))
    assert footage.main(["storyboard", str(draft)], session=Session(), sleep=lambda s: None) == 0
    ep["video_terms"].append("nothing")
    draft.write_text(json.dumps({"series_id": "2026-11-01-x", "date": "2026-11-01", "episodes": [ep]}))
    assert footage.main(["storyboard", str(draft)], session=Session(), sleep=lambda s: None) == 1
    assert "NO CLIP" in capsys.readouterr().out
