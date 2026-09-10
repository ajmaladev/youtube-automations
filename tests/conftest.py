"""Test isolation: no real .env, no credentials, no network."""
from __future__ import annotations

import socket

import pytest

from orchestrator import config

ENV_KEYS = [
    "PEXELS_API_KEY", "OPENAI_API_KEY", "OLLAMA_HOST", "OLLAMA_MODEL", "LLM_PROVIDER",
    "ELEVENLABS_API_KEY", "YOUTUBE_CLIENT_SECRETS_PATH", "MPT_BASE_URL", "MPT_API_KEY",
    "BASIC_AUTH_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_HASH", "CREDENTIALS_DIR",
    "OUTPUT_DIR", "TOPIC_QUEUE_PATH", "YOUTUBE_UPLOAD_UNIT_COST", "YOUTUBE_DAILY_QUOTA_UNITS",
    "YOUTUBE_MAX_UPLOADS_PER_DAY", "DAILY_GENERATE_COUNT", "MPT_POLL_INTERVAL_S",
    "MPT_GENERATE_TIMEOUT_S", "OLLAMA_BASE_URL",
]

QUEUE_YAML = """
defaults:
  voice_name: en-US-JennyNeural-Female
  tags: [shorts]
topics:
  - id: t1
    subject: Topic one
  - id: t2
    subject: Topic two
    title: Given title
    description: Given description
  - id: off
    subject: disabled
    enabled: false
"""


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORCH_ENV_FILE", str(tmp_path / "no-such.env"))

    def _blocked(*a, **k):
        raise NetworkBlocked("network access attempted in tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def settings(monkeypatch, tmp_path):
    queue = tmp_path / "queue.yaml"
    queue.write_text(QUEUE_YAML, encoding="utf-8")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path / ".credentials"))
    monkeypatch.setenv("TOPIC_QUEUE_PATH", str(queue))
    monkeypatch.setenv("MPT_POLL_INTERVAL_S", "0")
    return config.load()
