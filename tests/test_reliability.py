"""Reliability: broken-video detection, render retries, calendar-day renders and duplicate-safe uploads."""
import struct

import pytest

from orchestrator import config, generate, review, upload, video_check
from orchestrator.models import VideoRequest
from test_content_calendar import make_month, write
from test_generate import DONE, client_for
from test_upload import FakeService, approve_items, ledger

FAILED = {"state": -1, "failed_stage": "audio", "error": "edge tts timed out"}


# --- video checks -------------------------------------------------------------------------

def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def track(width, height, handler):
    tkhd = box(b"tkhd", bytes(4 + 20 + 52) + struct.pack(">II", width << 16, height << 16))
    return box(b"trak", tkhd + box(b"mdia", box(b"hdlr", bytes(8) + handler + bytes(12))))


def mp4_bytes(seconds=32.0, tracks=((1080, 1920, b"vide"), (0, 0, b"soun")), pad=400_000):
    mvhd = box(b"mvhd", bytes(4) + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + bytes(80))
    moov = box(b"moov", mvhd + b"".join(track(*t) for t in tracks))
    return box(b"ftyp", b"isom" + bytes(4)) + box(b"mdat", bytes(pad)) + moov


def test_check_video_accepts_a_complete_short(tmp_path):
    path = tmp_path / "ok.mp4"
    path.write_bytes(mp4_bytes())
    info = video_check.check_video(path, words=95, aspect="9:16")
    assert info.duration_s == 32.0 and info.frame_size == (1080, 1920)


@pytest.mark.parametrize("data, problem", [
    (b"MP4DATA", "too small"),
    (mp4_bytes()[:-200], "corrupt"),
    (box(b"ftyp", b"isom" + bytes(4)) + box(b"mdat", bytes(400_000)), "incomplete"),
    (mp4_bytes(tracks=((1080, 1920, b"vide"),)), "no audio"),
    (mp4_bytes(tracks=((1920, 1080, b"vide"), (0, 0, b"soun"))), "1920x1080"),
    (mp4_bytes(seconds=4.0), "too short"),
    (mp4_bytes(seconds=400.0), "too long"),
], ids=["tiny", "truncated", "no-moov", "no-audio", "landscape", "too-short", "too-long"])
def test_check_video_rejects_broken_renders(tmp_path, data, problem):
    path = tmp_path / "bad.mp4"
    path.write_bytes(data)
    with pytest.raises(video_check.VideoCheckError, match=problem):
        video_check.check_video(path, words=95, aspect="9:16")


# --- render retries -------------------------------------------------------------------------

@pytest.fixture
def retry_settings(settings, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "x")
    monkeypatch.setenv("OLLAMA_HOST", "http://h:11434")
    monkeypatch.setenv("GENERATE_ATTEMPTS", "3")
    monkeypatch.setenv("GENERATE_RETRY_DELAY_S", "0")
    return config.load()


def test_failed_render_is_retried_and_last_attempt_widens_footage(retry_settings):
    client, session = client_for(retry_settings, [FAILED, FAILED, DONE])
    req = VideoRequest.from_dict({"topic_id": "t", "subject": "s", "title": "T", "description": "D",
                                  "video_terms": "akita dog, tokyo station"})
    assert generate.generate_one(req, retry_settings, client).name == "t.mp4"
    terms = [body["video_terms"] for method, url, body in session.calls if method == "POST"]
    assert terms[:2] == ["akita dog, tokyo station"] * 2
    assert terms[2].startswith("akita dog, tokyo station, ") and "clouds timelapse" in terms[2]


def test_broken_video_is_deleted_and_rendered_again(retry_settings, monkeypatch):
    monkeypatch.setenv("VERIFY_VIDEOS", "true")
    settings = config.load()
    outcomes = iter([video_check.VideoCheckError("no audio track"), None])

    def fake_check(path, words=0, aspect="9:16"):
        problem = next(outcomes)
        if problem:
            raise problem
        return video_check.Mp4Info(30.0, [{"handler": "vide", "width": 1080, "height": 1920}])

    monkeypatch.setattr(video_check, "check_video", fake_check)
    client, _ = client_for(settings, [DONE, DONE])
    mp4 = generate.generate_one(VideoRequest(topic_id="t", subject="s", title="T", description="D"), settings, client)
    assert mp4.exists() and next(outcomes, "done") == "done"


def test_calendar_day_render_reports_parts_that_never_render(retry_settings, monkeypatch):
    monkeypatch.setenv("GENERATE_ATTEMPTS", "2")
    settings = config.load()
    write(settings.calendar_dir, "2026-10.json", make_month(["2026-10-01", "2026-10-02"]))
    client, _ = client_for(settings, [DONE, FAILED, FAILED, DONE])
    failed = []
    produced = generate.run(day="2026-10-01", settings=settings, client=client, failed=failed)
    assert [p.name for p in produced] == ["2026-10-01-reef-p1.mp4", "2026-10-01-reef-p3.mp4"]
    assert failed == ["2026-10-01-reef-p2"]
    assert generate.load_state(settings)["2026-10-01-reef-p2"]["status"] == "failed"


def test_calendar_day_render_can_pick_one_part_and_skips_uploaded_ones(retry_settings):
    settings = retry_settings
    write(settings.calendar_dir, "2026-10.json", make_month(["2026-10-01"]))
    (review.stage_dir(settings.output_dir, review.UPLOADED) / "2026-10-01-reef-p1.json").write_text("{}")
    client, _ = client_for(settings, [DONE])
    assert [p.name for p in generate.run(day="2026-10-01", part="p2", settings=settings, client=client)] == [
        "2026-10-01-reef-p2.mp4"]
    client, _ = client_for(settings, [DONE])  # p1 is already on YouTube and p2 was just rendered
    assert [p.name for p in generate.run(day="2026-10-01", settings=settings, client=client)] == [
        "2026-10-01-reef-p3.mp4"]


def test_calendar_day_without_episodes_stops(retry_settings):
    with pytest.raises(SystemExit, match="no content-calendar episodes"):
        generate.run(day="2026-10-01", settings=retry_settings, client=object())


def test_generate_exit_code_reflects_failures(settings, monkeypatch):
    monkeypatch.setattr(generate, "run", lambda **kw: kw["failed"].append("x") or [])
    assert generate.main(["--date", "2026-10-01"]) == 1
    monkeypatch.setattr(generate, "run", lambda **kw: [])
    assert generate.main([]) == 0


# --- uploads --------------------------------------------------------------------------------

@pytest.fixture
def up_settings(settings, monkeypatch, tmp_path):
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS_PATH", str(secrets))
    return config.load()


def test_upload_never_posts_an_episode_twice(up_settings, tmp_path):
    out = up_settings.output_dir
    approve_items(out, ["a", "b"])
    (review.stage_dir(out, review.UPLOADED) / "a.json").write_text("{}")  # upload history from an earlier run
    service = FakeService([[(None, {"id": "vid-b"})]])
    assert upload.run(settings=up_settings, service=service, ledger=ledger(tmp_path)) == ["vid-b"]
    assert len(service.bodies) == 1


def test_upload_exit_code_fails_when_approved_videos_are_left(up_settings, monkeypatch):
    approve_items(up_settings.output_dir, ["a"])
    monkeypatch.setattr(upload, "run", lambda **kw: [])
    assert upload.main([]) == 1
    (review.stage_dir(up_settings.output_dir, review.UPLOADED) / "a.json").write_text("{}")
    assert upload.main([]) == 0


def test_ci_never_opens_a_browser_for_youtube_login(up_settings, monkeypatch, tmp_path):
    monkeypatch.setenv("CI", "true")
    approve_items(up_settings.output_dir, ["a"])
    calls = []

    def fake_credentials(settings, interactive=True):
        calls.append(interactive)
        raise upload.UploadError("no valid token")

    monkeypatch.setattr(upload, "get_credentials", fake_credentials)
    with pytest.raises(upload.UploadError):
        upload.run(settings=up_settings, ledger=ledger(tmp_path))
    assert calls == [False]
