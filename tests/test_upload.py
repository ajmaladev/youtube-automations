import json
from datetime import datetime
from unittest import mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from orchestrator import config, review, upload
from orchestrator.models import VideoRequest, VideoResult


def http_error(status, reason=None):
    content = json.dumps({"error": {"errors": [{"reason": reason}] if reason else []}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


class FakeInsert:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def next_chunk(self):
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


class FakeService:
    def __init__(self, outcomes_per_upload):
        self.bodies = []
        self._outcomes = list(outcomes_per_upload)

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.bodies.append(body)
        return FakeInsert(self._outcomes.pop(0))


# --- metadata -------------------------------------------------------------------------

def test_body_is_private_with_synthetic_disclosure():
    body = upload.build_body(VideoRequest(topic_id="a", subject="Subject <b>", tags=["#shorts", "ai"]))
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": True}
    assert body["snippet"]["title"] == "Subject b"
    assert body["snippet"]["tags"] == ["shorts", "ai"]


def test_body_refuses_public_even_if_mutated():
    req = VideoRequest(topic_id="a", subject="s")
    req.privacy_status = "public"
    with pytest.raises(upload.UploadError):
        upload.build_body(req)


def test_title_and_tags_are_truncated_to_youtube_limits():
    req = VideoRequest(topic_id="a", subject="x" * 300, tags=["t" * 40] * 30)
    body = upload.build_body(req)
    assert len(body["snippet"]["title"]) == 100
    assert sum(len(t) for t in body["snippet"]["tags"]) + len(body["snippet"]["tags"]) <= 501


# --- quota ----------------------------------------------------------------------------

def ledger(tmp_path, when="2026-09-10T09:00:00-07:00", **kw):
    now = datetime.fromisoformat(when)
    args = dict(unit_cost=1600, daily_units=10_000, max_uploads=5) | kw
    return upload.QuotaLedger(tmp_path / "ledger.json", now=lambda: now, **args)


def test_quota_hard_cap_is_five_per_day(tmp_path):
    lg = ledger(tmp_path)
    assert lg.remaining_uploads() == 5  # 10000 // 1600 = 6, capped at 5
    for _ in range(5):
        lg.record_attempt()
    assert lg.remaining_uploads() == 0 and lg.remaining_units() == 2000
    assert ledger(tmp_path, max_uploads=50).max_uploads == 5


def test_quota_units_limit_when_cost_is_higher(tmp_path):
    lg = ledger(tmp_path, unit_cost=4000)
    assert lg.remaining_uploads() == 2


def test_quota_resets_at_pacific_midnight(tmp_path):
    lg = ledger(tmp_path, when="2026-09-10T23:59:00-07:00")
    for _ in range(5):
        lg.record_attempt()
    # 07:30 UTC on the 11th is still the 10th in Pacific time
    assert ledger(tmp_path, when="2026-09-11T06:30:00+00:00").remaining_uploads() == 0
    assert ledger(tmp_path, when="2026-09-11T00:01:00-07:00").remaining_uploads() == 5


# --- resumable upload -------------------------------------------------------------------

class Status:
    def progress(self):
        return 0.5


def test_resumable_upload_retries_with_backoff():
    req = FakeInsert([http_error(503), ConnectionError("reset"), (Status(), None), (None, {"id": "v1"})])
    sleeps = []
    assert upload.resumable_upload(req, sleep=sleeps.append, rand=lambda: 1.0)["id"] == "v1"
    assert sleeps == [2.0, 4.0]


def test_resumable_upload_stops_on_quota_error():
    with pytest.raises(upload.QuotaExceededError):
        upload.resumable_upload(FakeInsert([http_error(403, "quotaExceeded")]), sleep=lambda s: None)


def test_resumable_upload_non_retriable_and_exhausted():
    with pytest.raises(upload.UploadError, match="HTTP 400"):
        upload.resumable_upload(FakeInsert([http_error(400, "invalidTitle")]), sleep=lambda s: None)
    with pytest.raises(upload.UploadError, match="giving up"):
        upload.resumable_upload(FakeInsert([http_error(500)] * 4), sleep=lambda s: None, max_retries=3)


# --- run ------------------------------------------------------------------------------

def approve_items(output_dir, ids):
    for tid in ids:
        pending = review.stage_dir(output_dir, review.PENDING)
        mp4 = pending / f"{tid}.mp4"
        mp4.write_bytes(b"data-" + tid.encode())
        review.stage_pending(VideoResult(request=VideoRequest(topic_id=tid, subject=tid),
                                         task_id="t", video_path=str(mp4)), output_dir)
        review.approve(tid, output_dir)


@pytest.fixture
def up_settings(settings, monkeypatch, tmp_path):
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS_PATH", str(secrets))
    return config.load()


def test_run_uploads_only_approved_and_respects_cap(up_settings, tmp_path):
    out = up_settings.output_dir
    approve_items(out, [f"a{i}" for i in range(7)])
    # one pending item that must never be touched
    (review.stage_dir(out, review.PENDING) / "p.mp4").write_bytes(b"p")
    review.stage_pending(VideoResult(request=VideoRequest(topic_id="p", subject="p"), task_id="t",
                                     video_path=str(out / "pending" / "p.mp4")), out)
    service = FakeService([[(None, {"id": f"vid{i}"})] for i in range(7)])
    lg = ledger(tmp_path)
    ids = upload.run(settings=up_settings, service=service, ledger=lg)
    assert ids == [f"vid{i}" for i in range(5)]
    assert all(b["status"]["privacyStatus"] == "private" for b in service.bodies)
    assert len(review.list_items(out, review.UPLOADED)) == 5
    assert len(review.list_items(out, review.APPROVED)) == 2
    assert (out / "pending" / "p.json").exists()
    assert lg.remaining_uploads() == 0


def test_run_stops_on_quota_exceeded(up_settings, tmp_path):
    approve_items(up_settings.output_dir, ["a", "b"])
    service = FakeService([[http_error(403, "quotaExceeded")], [(None, {"id": "never"})]])
    assert upload.run(settings=up_settings, service=service, ledger=ledger(tmp_path)) == []
    assert len(service.bodies) == 1


def test_run_dry_run_calls_nothing(settings, tmp_path, caplog):
    caplog.set_level("INFO")
    approve_items(settings.output_dir, ["a"])
    with mock.patch.object(upload, "get_credentials") as creds, mock.patch.object(upload, "build_service") as svc:
        assert upload.run(dry_run=True, settings=settings, ledger=ledger(tmp_path)) == []
    creds.assert_not_called()
    svc.assert_not_called()
    assert "DRY-RUN: would upload a.mp4" in caplog.text
    assert not (tmp_path / "ledger.json").exists()


def test_run_without_client_secrets_fails_loudly(settings):
    with pytest.raises(config.ConfigError):
        upload.run(settings=settings)


# --- OAuth ------------------------------------------------------------------------------

def test_expired_token_is_refreshed_and_persisted(up_settings):
    fake = mock.MagicMock(valid=False, expired=True, refresh_token="r")
    fake.to_json.return_value = '{"token": "new"}'
    tok = upload.token_path(up_settings)
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text("{}")
    with mock.patch.object(upload.Credentials, "from_authorized_user_file", return_value=fake), \
         mock.patch.object(upload, "InstalledAppFlow") as flow:
        assert upload.get_credentials(up_settings) is fake
    fake.refresh.assert_called_once()
    flow.from_client_secrets_file.assert_not_called()
    assert tok.read_text() == '{"token": "new"}'


def test_missing_token_runs_desktop_flow_with_offline_access(up_settings):
    creds = mock.MagicMock()
    creds.to_json.return_value = "{}"
    with mock.patch.object(upload, "InstalledAppFlow") as flow:
        flow.from_client_secrets_file.return_value.run_local_server.return_value = creds
        upload.get_credentials(up_settings)
    kwargs = flow.from_client_secrets_file.return_value.run_local_server.call_args.kwargs
    assert kwargs["access_type"] == "offline" and kwargs["prompt"] == "consent"
    assert flow.from_client_secrets_file.call_args.args[1] == upload.SCOPES
    assert upload.token_path(up_settings).is_file()


def test_non_interactive_without_token_raises(up_settings):
    with pytest.raises(upload.UploadError):
        upload.get_credentials(up_settings, interactive=False)
