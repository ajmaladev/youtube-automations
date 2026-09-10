"""Load settings from .env / environment and validate them per pipeline stage.

Values are never logged. `require()` raises ConfigError listing every missing
key at once so misconfiguration fails loudly instead of halfway through a run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    mpt_base_url: str
    mpt_api_key: str
    basic_auth_user: str
    basic_auth_password: str
    pexels_api_key: str
    openai_api_key: str
    ollama_host: str
    llm_provider: str
    elevenlabs_api_key: str
    youtube_client_secrets_path: str
    credentials_dir: Path
    output_dir: Path
    queue_path: Path
    upload_unit_cost: int
    daily_quota_units: int
    max_uploads_per_day: int
    daily_generate_count: int
    poll_interval_s: float
    generate_timeout_s: float

    # --- per-stage validation -------------------------------------------------
    def missing_for(self, stage: str) -> list[str]:
        missing: list[str] = []
        if stage == "generate":
            if not self.pexels_api_key:
                missing.append("PEXELS_API_KEY")
            if self.llm_provider == "openai" and not self.openai_api_key:
                missing.append("OPENAI_API_KEY")
            if self.llm_provider == "ollama" and not self.ollama_host:
                missing.append("OLLAMA_HOST")
            if not self.mpt_base_url:
                missing.append("MPT_BASE_URL")
        elif stage == "upload":
            if not self.youtube_client_secrets_path:
                missing.append("YOUTUBE_CLIENT_SECRETS_PATH")
            elif not Path(self.youtube_client_secrets_path).is_file():
                missing.append(
                    f"YOUTUBE_CLIENT_SECRETS_PATH (file not found: {self.youtube_client_secrets_path})"
                )
        elif stage != "review":
            raise ValueError(f"unknown stage {stage!r}")
        return missing

    def require(self, stage: str, dry_run: bool = False) -> None:
        missing = self.missing_for(stage)
        if not missing:
            return
        msg = f"[{stage}] missing required configuration: {', '.join(missing)} (see .env.example)"
        if dry_run:
            log.warning("DRY-RUN: %s", msg)
            return
        raise ConfigError(msg)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def load(env_file: str | os.PathLike | None = None) -> Settings:
    """Read settings. `env_file` defaults to $ORCH_ENV_FILE or <repo>/.env."""
    path = Path(env_file or os.getenv("ORCH_ENV_FILE") or REPO_ROOT / ".env")
    if path.is_file():
        load_dotenv(path, override=False)

    def s(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    max_uploads = _int("YOUTUBE_MAX_UPLOADS_PER_DAY", 5)
    if max_uploads > 5:
        raise ConfigError("YOUTUBE_MAX_UPLOADS_PER_DAY is hard-capped at 5")

    return Settings(
        mpt_base_url=s("MPT_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
        mpt_api_key=s("MPT_API_KEY"),
        basic_auth_user=s("BASIC_AUTH_USER"),
        basic_auth_password=s("BASIC_AUTH_PASSWORD"),
        pexels_api_key=s("PEXELS_API_KEY"),
        openai_api_key=s("OPENAI_API_KEY"),
        ollama_host=s("OLLAMA_HOST"),
        llm_provider=s("LLM_PROVIDER", "ollama").lower(),
        elevenlabs_api_key=s("ELEVENLABS_API_KEY"),
        youtube_client_secrets_path=s("YOUTUBE_CLIENT_SECRETS_PATH"),
        credentials_dir=Path(s("CREDENTIALS_DIR") or REPO_ROOT / ".credentials"),
        output_dir=Path(s("OUTPUT_DIR") or REPO_ROOT / "output"),
        queue_path=Path(s("TOPIC_QUEUE_PATH") or REPO_ROOT / "topics" / "queue.yaml"),
        upload_unit_cost=_int("YOUTUBE_UPLOAD_UNIT_COST", 1600),
        daily_quota_units=_int("YOUTUBE_DAILY_QUOTA_UNITS", 10_000),
        max_uploads_per_day=max_uploads,
        daily_generate_count=_int("DAILY_GENERATE_COUNT", 1),
        poll_interval_s=_float("MPT_POLL_INTERVAL_S", 10.0),
        generate_timeout_s=_float("MPT_GENERATE_TIMEOUT_S", 1800.0),
    )


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
