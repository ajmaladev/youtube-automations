"""Upload approved videos with the YouTube Data API v3.

- OAuth 2.0 installed-app (desktop) flow; token refreshed and persisted to
  .credentials/youtube.token.json
- Resumable upload with exponential backoff + jitter on retriable errors
- status.containsSyntheticMedia = True (altered/synthetic content disclosure)
- privacyStatus "private" by default; "public" is refused
- Quota ledger keyed by Pacific-time date (quota resets at midnight PT):
  YOUTUBE_UPLOAD_UNIT_COST per attempt against YOUTUBE_DAILY_QUOTA_UNITS,
  and a hard cap of 5 upload attempts/day regardless of units

Only reads from output/approved/ (via review.load_approved).

CLI: python -m orchestrator.upload [--dry-run] [--auth-only]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from orchestrator import config, review
from orchestrator.models import ALLOWED_PRIVACY, VideoRequest

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILENAME = "youtube.token.json"
HARD_MAX_UPLOADS_PER_DAY = 5
PACIFIC = ZoneInfo("America/Los_Angeles")

CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 10
MAX_BACKOFF_S = 64.0
RETRIABLE_STATUS = {500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, ConnectionError, TimeoutError, OSError)
QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"}


class QuotaExceededError(RuntimeError):
    pass


class UploadError(RuntimeError):
    pass


# --- quota ------------------------------------------------------------------------

class QuotaLedger:
    """Local, conservative record of quota spent today (Pacific time)."""

    def __init__(self, path: Path, unit_cost: int, daily_units: int, max_uploads: int,
                 now: Callable[[], datetime] = lambda: datetime.now(PACIFIC)):
        self.path = Path(path)
        self.unit_cost = unit_cost
        self.daily_units = daily_units
        self.max_uploads = min(max_uploads, HARD_MAX_UPLOADS_PER_DAY)
        self._now = now

    def _today(self) -> str:
        return self._now().astimezone(PACIFIC).date().isoformat()

    def _load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}

    def _day(self) -> dict[str, Any]:
        return self._load().get(self._today(), {"units": 0, "attempts": 0, "video_ids": []})

    def used_units(self) -> int:
        return int(self._day()["units"])

    def attempts_today(self) -> int:
        return int(self._day()["attempts"])

    def remaining_units(self) -> int:
        return max(0, self.daily_units - self.used_units())

    def remaining_uploads(self) -> int:
        by_units = self.remaining_units() // self.unit_cost if self.unit_cost > 0 else self.max_uploads
        return max(0, min(self.max_uploads - self.attempts_today(), by_units))

    def log_budget(self) -> None:
        log.info(
            "quota (%s PT): %d/%d units used, %d/%d uploads today -> %d units left, %d upload(s) allowed",
            self._today(), self.used_units(), self.daily_units, self.attempts_today(),
            self.max_uploads, self.remaining_units(), self.remaining_uploads(),
        )

    def record_attempt(self) -> None:
        """Charged BEFORE the request: failed inserts can still consume quota."""
        data = self._load()
        day = data.setdefault(self._today(), {"units": 0, "attempts": 0, "video_ids": []})
        day["units"] += self.unit_cost
        day["attempts"] += 1
        self._save(data)

    def record_success(self, video_id: str) -> None:
        data = self._load()
        data.setdefault(self._today(), {"units": 0, "attempts": 0, "video_ids": []})["video_ids"].append(video_id)
        self._save(data)

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def ledger_for(settings: config.Settings) -> QuotaLedger:
    return QuotaLedger(settings.output_dir / "quota_ledger.json", settings.upload_unit_cost,
                       settings.daily_quota_units, settings.max_uploads_per_day)


# --- OAuth ------------------------------------------------------------------------

def token_path(settings: config.Settings) -> Path:
    return settings.credentials_dir / TOKEN_FILENAME


def _save_token(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_credentials(settings: config.Settings, interactive: bool = True) -> Credentials:
    path = token_path(settings)
    creds: Credentials | None = None

    # GitHub Actions / headless: token JSON in env (same pattern as startups repo).
    token_json = (os.environ.get("YOUTUBE_TOKEN_JSON") or "").strip()
    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
            log.info("loaded YouTube credentials from YOUTUBE_TOKEN_JSON")
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("YOUTUBE_TOKEN_JSON could not be parsed: %s", exc)

    if creds is None and path.is_file():
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, path)
            log.info("refreshed YouTube OAuth token")
            return creds
        except RefreshError as exc:
            # Typical cause: consent screen in "Testing" mode (7-day refresh tokens) or revoked access.
            log.error("token refresh failed (%s); re-authorization required", exc)
    if not interactive:
        raise UploadError(f"no valid token at {path}; run `python -m orchestrator.upload --auth-only`")

    secrets_json = (
        os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON")
        or os.environ.get("CLIENT_SECRETS_JSON")
        or ""
    ).strip()
    if secrets_json:
        flow = InstalledAppFlow.from_client_config(json.loads(secrets_json), SCOPES)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(settings.youtube_client_secrets_path, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    _save_token(creds, path)
    log.info("saved new YouTube OAuth token to %s", path)
    return creds


def build_service(creds: Credentials):
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# --- metadata ---------------------------------------------------------------------

def _clean_title(title: str) -> str:
    title = " ".join(title.replace("<", "").replace(">", "").split())
    return title[:100] or "Untitled"


def _clean_description(desc: str) -> str:
    desc = desc.replace("<", "").replace(">", "")
    raw = desc.encode("utf-8")[:5000]
    return raw.decode("utf-8", errors="ignore")


def _clean_tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = t.replace("<", "").replace(">", "").lstrip("#").strip()
        if not t:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > 500:
            break
        out.append(t)
        total += cost
    return out


def build_body(req: VideoRequest) -> dict[str, Any]:
    privacy = req.privacy_status or "private"
    if privacy not in ALLOWED_PRIVACY:
        raise UploadError(f"refusing privacyStatus={privacy!r}; only {ALLOWED_PRIVACY} are allowed")
    return {
        "snippet": {
            "title": _clean_title(req.title or req.subject),
            "description": _clean_description(req.description),
            "tags": _clean_tags(req.tags),
            "categoryId": str(req.category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(req.made_for_kids),
            "containsSyntheticMedia": True,  # AI-generated: always disclose
        },
    }


# --- upload -----------------------------------------------------------------------

def _quota_reason(err: HttpError) -> str | None:
    try:
        details = json.loads(err.content.decode("utf-8"))["error"]["errors"]
        reasons = {d.get("reason") for d in details}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    hit = reasons & QUOTA_REASONS
    return next(iter(hit)) if hit else None


def resumable_upload(insert_request, sleep: Callable[[float], None] = time.sleep,
                     rand: Callable[[], float] = random.random, max_retries: int = MAX_RETRIES) -> dict:
    response, retry = None, 0
    while response is None:
        error: str | None = None
        try:
            status, response = insert_request.next_chunk()
            if status is not None:
                log.info("upload progress: %d%%", int(status.progress() * 100))
        except HttpError as exc:
            code = getattr(exc.resp, "status", None)
            reason = _quota_reason(exc)
            if code == 403 and reason:
                raise QuotaExceededError(f"YouTube rejected upload: {reason}") from exc
            if code in RETRIABLE_STATUS:
                error = f"retriable HTTP {code}"
            else:
                raise UploadError(f"HTTP {code}: {exc}") from exc
        except RETRIABLE_EXCEPTIONS as exc:
            error = f"retriable error: {exc!r}"

        if error is not None:
            retry += 1
            if retry > max_retries:
                raise UploadError(f"giving up after {max_retries} retries ({error})")
            delay = min(MAX_BACKOFF_S, 2 ** retry) * (0.5 + rand() / 2)
            log.warning("%s; retry %d/%d in %.1fs", error, retry, max_retries, delay)
            sleep(delay)

    if "id" not in response:
        raise UploadError(f"unexpected upload response: {response}")
    return response


def upload_video(service, mp4: Path, req: VideoRequest, sleep: Callable[[float], None] = time.sleep) -> str:
    body = build_body(req)
    media = MediaFileUpload(str(mp4), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True)
    insert_request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = resumable_upload(insert_request, sleep=sleep)
    log.info("uploaded %s -> https://youtu.be/%s (privacy=%s)", mp4.name, response["id"],
             body["status"]["privacyStatus"])
    return response["id"]


def run(dry_run: bool = False, settings: config.Settings | None = None,
        service=None, ledger: QuotaLedger | None = None) -> list[str]:
    settings = settings or config.load()
    settings.require("upload", dry_run=dry_run)
    ledger = ledger or ledger_for(settings)
    ledger.log_budget()

    items = review.load_approved(settings.output_dir)
    if not items:
        log.info("nothing in %s/ - approve items with `make review` first", review.APPROVED)
        return []

    allowed = ledger.remaining_uploads()
    uploaded: list[str] = []
    for i, (sidecar, result) in enumerate(items):
        if i >= allowed:
            log.warning("daily upload budget reached; %d approved item(s) left for tomorrow", len(items) - i)
            break
        mp4 = review.video_file(sidecar, result)
        body = build_body(result.request)  # validates privacy even in dry-run
        if dry_run:
            log.info("DRY-RUN: would upload %s (%d bytes) with %s", mp4.name, mp4.stat().st_size,
                     json.dumps(body, ensure_ascii=False))
            log.info("DRY-RUN: would charge %d units and move sidecar to %s/", settings.upload_unit_cost,
                     review.UPLOADED)
            continue
        if service is None:
            service = build_service(get_credentials(settings))
        ledger.record_attempt()
        try:
            video_id = upload_video(service, mp4, result.request)
        except QuotaExceededError as exc:
            log.error("%s - stopping for today", exc)
            break
        except UploadError as exc:
            log.error("upload of %s failed: %s", mp4.name, exc)
            continue
        finally:
            ledger.log_budget()
        ledger.record_success(video_id)
        review.mark_uploaded(sidecar, result, video_id, settings.output_dir)
        uploaded.append(video_id)
    return uploaded


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.upload", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="log what would be uploaded; no API calls")
    p.add_argument("--auth-only", action="store_true", help="run/refresh OAuth and save the token, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)
    settings = config.load()
    if args.auth_only:
        if args.dry_run:
            log.info("DRY-RUN: would run the OAuth desktop flow and write %s", token_path(settings))
            return 0
        settings.require("upload")
        get_credentials(settings)
        return 0
    run(dry_run=args.dry_run, settings=settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
