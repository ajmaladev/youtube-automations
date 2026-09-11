import importlib
import tomllib

import pytest

from orchestrator import config, render_config
from orchestrator.config import REPO_ROOT
from orchestrator.models import VideoRequest

MODULES = ["config", "models", "generate", "upload", "review", "scheduler", "render_config"]


@pytest.mark.parametrize("name", MODULES)
def test_every_module_imports_without_credentials(name):
    importlib.import_module(f"orchestrator.{name}")


def test_load_defaults_without_env():
    s = config.load()
    assert s.engine_base_url == "http://127.0.0.1:8080"
    assert s.llm_provider == "ollama"
    assert s.max_uploads_per_day == 5
    assert s.upload_unit_cost == 1600


def test_require_fails_loudly_listing_all_missing(settings):
    with pytest.raises(config.ConfigError) as exc:
        settings.require("generate")
    assert "PEXELS_API_KEY" in str(exc.value) and "OLLAMA_HOST" in str(exc.value)
    with pytest.raises(config.ConfigError, match="YOUTUBE_CLIENT_SECRETS_PATH"):
        settings.require("upload")


def test_require_only_warns_in_dry_run(settings, caplog):
    settings.require("generate", dry_run=True)
    settings.require("upload", dry_run=True)
    assert "missing required configuration" in caplog.text


def test_upload_requires_existing_secrets_file(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRETS_JSON", raising=False)
    monkeypatch.delenv("CLIENT_SECRETS_JSON", raising=False)
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS_PATH", str(tmp_path / "nope.json"))
    missing = config.load().missing_for("upload")
    assert any("YOUTUBE_CLIENT_SECRETS" in m for m in missing)
    (tmp_path / "nope.json").write_text("{}")
    assert config.load().missing_for("upload") == []


def test_openai_provider_requires_openai_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("PEXELS_API_KEY", "x")
    assert config.load().missing_for("generate") == ["OPENAI_API_KEY"]


def test_upload_cap_cannot_exceed_five(monkeypatch):
    monkeypatch.setenv("YOUTUBE_MAX_UPLOADS_PER_DAY", "6")
    with pytest.raises(config.ConfigError):
        config.load()


# --- render_config ------------------------------------------------------------------

TEMPLATE = (REPO_ROOT / "config" / "config.template.toml").read_text(encoding="utf-8")


def test_template_renders_free_stack_with_no_env():
    cfg = tomllib.loads(render_config.render(TEMPLATE, {}))
    app = cfg["app"]
    assert app["llm_provider"] == "ollama"
    assert app["video_source"] == "pexels"
    assert app["subtitle_provider"] == "edge"
    assert app["pexels_api_keys"] == []
    assert app["api_key"] == "" and app["openai_api_key"] == ""
    assert cfg["ui"]["tts_server"] == "azure-tts-v1"
    assert "${" not in render_config.render(TEMPLATE, {}).split("\n", 5)[-1].replace("${VAR}", "")


def test_template_renders_env_values_safely():
    env = {"PEXELS_API_KEY": "k1, k2", "OPENAI_API_KEY": 'we"ird\\key',
           "OLLAMA_HOST": "http://host.docker.internal:11434", "ELEVENLABS_API_KEY": "el"}
    cfg = tomllib.loads(render_config.render(TEMPLATE, env))
    assert cfg["app"]["pexels_api_keys"] == ["k1", "k2"]
    assert cfg["app"]["openai_api_key"] == 'we"ird\\key'
    assert cfg["app"]["ollama_base_url"] == "http://host.docker.internal:11434/v1"
    assert cfg["elevenlabs"]["api_key"] == "el"


def test_render_main_writes_file(tmp_path, monkeypatch, capsys):
    src, dst = tmp_path / "t.toml", tmp_path / "out.toml"
    src.write_text('a = "${SECRET_X}"\n', encoding="utf-8")
    monkeypatch.setenv("SECRET_X", "s3cret")
    assert render_config.main(["x", str(src), str(dst)]) == 0
    assert tomllib.loads(dst.read_text())["a"] == "s3cret"
    assert "s3cret" not in capsys.readouterr().out


# --- models ---------------------------------------------------------------------------

def test_public_privacy_is_refused():
    with pytest.raises(ValueError):
        VideoRequest(topic_id="a", subject="b", privacy_status="public")


def test_unknown_keys_are_forwarded_to_engine():
    req = VideoRequest.from_dict({"topic_id": "a", "subject": "b", "font_size": 72})
    payload = req.to_engine_payload()
    assert payload["font_size"] == 72 and payload["video_subject"] == "b"
    assert payload["bgm_type"] == ""
