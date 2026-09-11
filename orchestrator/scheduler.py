"""Daily / slot run entrypoint.

GitHub Actions runs three slots per day:
  morning   -> plan a new 3-part series if needed, generate Part 1
  afternoon -> generate Part 2
  night     -> generate Part 3

Each slot generates exactly one episode, optionally auto-approves, then uploads.

CLI: python -m orchestrator.scheduler [--dry-run] [--skip-generate] [--skip-upload]
     [--auto-approve] [--series-only] [--slot morning|afternoon|night|auto] [--at HH:MM]
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orchestrator import config, generate, review, upload

log = logging.getLogger(__name__)

SLOTS = ("morning", "afternoon", "night")
# UTC hours used when --slot auto (GitHub Actions cron is UTC).
SLOT_HOURS_UTC = {
    "morning": range(5, 12),      # 05:00-11:59 UTC
    "afternoon": range(12, 18),   # 12:00-17:59 UTC
    "night": range(18, 24),       # 18:00-23:59 UTC
}


def detect_slot(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone_utc())
    hour = now.astimezone(timezone_utc()).hour
    for name, hours in SLOT_HOURS_UTC.items():
        if hour in hours:
            return name
    return "morning"


def timezone_utc():
    return ZoneInfo("UTC")


def bootstrap_youtube_files(settings: config.Settings) -> None:
    """Materialize OAuth files from env secrets (GitHub Actions)."""
    settings.credentials_dir.mkdir(parents=True, exist_ok=True)
    token_json = (os.environ.get("YOUTUBE_TOKEN_JSON") or "").strip()
    token_path = settings.credentials_dir / upload.TOKEN_FILENAME
    if token_json and not token_path.is_file():
        token_path.write_text(token_json, encoding="utf-8")
        log.info("wrote %s from YOUTUBE_TOKEN_JSON", token_path)
    secrets_json = (
        os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON")
        or os.environ.get("CLIENT_SECRETS_JSON")
        or ""
    ).strip()
    if secrets_json:
        path = Path_safe(settings.youtube_client_secrets_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_text(secrets_json, encoding="utf-8")
            log.info("wrote client secrets from env")


def Path_safe(value: str):
    from pathlib import Path
    return Path(value)


def run_daily(
    dry_run: bool = False,
    skip_generate: bool = False,
    skip_upload: bool = False,
    auto_approve: bool = False,
    series_only: bool = False,
    count: int = 1,
    settings: config.Settings | None = None,
    client=None,
    service=None,
) -> dict:
    settings = settings or config.load()
    bootstrap_youtube_files(settings)
    summary: dict = {"generated": [], "approved": [], "uploaded": [], "errors": [], "slot": None}

    if not skip_generate:
        try:
            paths = generate.run(
                count=count,
                dry_run=dry_run,
                settings=settings,
                client=client,
                series_only=series_only,
            )
            summary["generated"] = [str(p) for p in paths]
        except Exception as exc:  # keep going: approved uploads shouldn't wait on generation
            log.exception("generate stage failed")
            summary["errors"].append(f"generate: {exc}")

    if auto_approve:
        try:
            summary["approved"] = review.approve_pending(settings.output_dir, dry_run=dry_run)
        except Exception as exc:
            log.exception("auto-approve failed")
            summary["errors"].append(f"approve: {exc}")

    if not skip_upload:
        try:
            summary["uploaded"] = upload.run(dry_run=dry_run, settings=settings, service=service)
        except Exception as exc:
            log.exception("upload stage failed")
            summary["errors"].append(f"upload: {exc}")

    log.info(
        "daily run done: %d generated, %d approved, %d uploaded, %d error(s)",
        len(summary["generated"]),
        len(summary["approved"]),
        len(summary["uploaded"]),
        len(summary["errors"]),
    )
    return summary


def run_slot(
    slot: str = "auto",
    dry_run: bool = False,
    skip_generate: bool = False,
    skip_upload: bool = False,
    auto_approve: bool = True,
    settings: config.Settings | None = None,
    client=None,
    service=None,
) -> dict:
    """One episode per slot: morning plans if needed; afternoon/night continue the series."""
    resolved = detect_slot() if slot == "auto" else slot
    if resolved not in SLOTS:
        raise SystemExit(f"unknown slot {slot!r}; choose from {SLOTS} or auto")
    log.info("running slot=%s (series-only, count=1, auto_approve=%s)", resolved, auto_approve)
    summary = run_daily(
        dry_run=dry_run,
        skip_generate=skip_generate,
        skip_upload=skip_upload,
        auto_approve=auto_approve,
        series_only=True,
        count=1,
        settings=settings,
        client=client,
        service=service,
    )
    summary["slot"] = resolved
    return summary


def seconds_until(hhmm: str, now: datetime | None = None) -> float:
    now = now or datetime.now()
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="orchestrator.scheduler",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", help="log every step without calling any API")
    p.add_argument("--skip-generate", action="store_true")
    p.add_argument("--skip-upload", action="store_true")
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help="move pending videos to approved before upload (required on GitHub Actions)",
    )
    p.add_argument(
        "--series-only",
        action="store_true",
        help="ignore manual topics/queue.yaml entries; only series episodes",
    )
    p.add_argument(
        "--slot",
        choices=[*SLOTS, "auto"],
        help="morning/afternoon/night episode slot (auto = detect from UTC hour)",
    )
    p.add_argument("--count", type=int, default=1, help="videos to generate when not using --slot")
    p.add_argument("--at", metavar="HH:MM", help="stay running and fire every day at this local time")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)

    def once() -> dict:
        if args.slot:
            return run_slot(
                slot=args.slot,
                dry_run=args.dry_run,
                skip_generate=args.skip_generate,
                skip_upload=args.skip_upload,
                auto_approve=args.auto_approve or bool(os.environ.get("GITHUB_ACTIONS")),
            )
        return run_daily(
            dry_run=args.dry_run,
            skip_generate=args.skip_generate,
            skip_upload=args.skip_upload,
            auto_approve=args.auto_approve or bool(os.environ.get("GITHUB_ACTIONS")),
            series_only=args.series_only,
            count=args.count,
        )

    if not args.at:
        summary = once()
        return 1 if summary["errors"] else 0

    while True:
        wait = seconds_until(args.at)
        log.info("next run at %s (in %.0f min)", args.at, wait / 60)
        time.sleep(wait)
        once()


if __name__ == "__main__":
    raise SystemExit(main())
