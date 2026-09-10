from datetime import datetime

import pytest

from orchestrator import generate, review, scheduler, upload


def test_run_daily_dry_run_touches_nothing(settings, caplog):
    caplog.set_level("INFO")
    summary = scheduler.run_daily(dry_run=True, settings=settings)
    assert summary == {"generated": [], "uploaded": [], "errors": []}
    assert "DRY-RUN: would POST" in caplog.text
    assert review.list_items(settings.output_dir, review.PENDING) == []


def test_run_daily_upload_runs_even_if_generate_fails(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(generate, "run", lambda **k: (_ for _ in ()).throw(RuntimeError("mpt down")))
    monkeypatch.setattr(upload, "run", lambda **k: calls.append(k) or ["vid"])
    summary = scheduler.run_daily(settings=settings)
    assert summary["uploaded"] == ["vid"] and "mpt down" in summary["errors"][0]
    assert calls


def test_skip_flags(settings, monkeypatch):
    monkeypatch.setattr(generate, "run", lambda **k: pytest.fail("generate should be skipped"))
    monkeypatch.setattr(upload, "run", lambda **k: pytest.fail("upload should be skipped"))
    assert scheduler.run_daily(skip_generate=True, skip_upload=True, settings=settings)["errors"] == []


def test_seconds_until():
    now = datetime(2026, 9, 10, 8, 0)
    assert scheduler.seconds_until("09:30", now) == 5400
    assert scheduler.seconds_until("07:00", now) == 23 * 3600


@pytest.mark.parametrize("main, argv", [
    (generate.main, ["--dry-run"]),
    (review.main, ["--dry-run", "list"]),
    (upload.main, ["--dry-run"]),
    (upload.main, ["--dry-run", "--auth-only"]),
    (scheduler.main, ["--dry-run"]),
])
def test_every_entrypoint_supports_dry_run_without_credentials(settings, main, argv):
    assert main(argv) == 0
