"""Daily run entrypoint.

1. Generate DAILY_GENERATE_COUNT topics from topics/queue.yaml -> output/pending/
2. Upload whatever is in output/approved/ (never pending/), within quota

Run once per day from cron / Windows Task Scheduler, or keep it running with
`--at HH:MM` (local time).

CLI: python -m orchestrator.scheduler [--dry-run] [--skip-generate] [--skip-upload] [--at HH:MM]
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta

from orchestrator import config, generate, upload

log = logging.getLogger(__name__)


def run_daily(dry_run: bool = False, skip_generate: bool = False, skip_upload: bool = False,
              settings: config.Settings | None = None, client=None, service=None) -> dict:
    settings = settings or config.load()
    summary: dict = {"generated": [], "uploaded": [], "errors": []}

    if not skip_generate:
        try:
            summary["generated"] = [str(p) for p in generate.run(dry_run=dry_run, settings=settings, client=client)]
        except Exception as exc:  # keep going: approved uploads shouldn't wait on generation
            log.exception("generate stage failed")
            summary["errors"].append(f"generate: {exc}")

    if not skip_upload:
        try:
            summary["uploaded"] = upload.run(dry_run=dry_run, settings=settings, service=service)
        except Exception as exc:
            log.exception("upload stage failed")
            summary["errors"].append(f"upload: {exc}")

    log.info("daily run done: %d generated, %d uploaded, %d error(s)",
             len(summary["generated"]), len(summary["uploaded"]), len(summary["errors"]))
    return summary


def seconds_until(hhmm: str, now: datetime | None = None) -> float:
    now = now or datetime.now()
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.scheduler", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="log every step without calling any API")
    p.add_argument("--skip-generate", action="store_true")
    p.add_argument("--skip-upload", action="store_true")
    p.add_argument("--at", metavar="HH:MM", help="stay running and fire every day at this local time")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)

    if not args.at:
        summary = run_daily(args.dry_run, args.skip_generate, args.skip_upload)
        return 1 if summary["errors"] else 0

    while True:
        wait = seconds_until(args.at)
        log.info("next run at %s (in %.0f min)", args.at, wait / 60)
        time.sleep(wait)
        run_daily(args.dry_run, args.skip_generate, args.skip_upload)


if __name__ == "__main__":
    raise SystemExit(main())
