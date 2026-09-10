import json

import pytest

from orchestrator import generate, review


class FakeResp:
    def __init__(self, status_code=200, body=None, content=b"", method="GET", url="http://mpt"):
        self.status_code = status_code
        self._body = body
        self._content = content
        self.text = json.dumps(body) if body is not None else ""
        self.url = url
        self.request = type("R", (), {"method": method})()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, chunk_size=1):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Mimics MPT: POST /api/v1/videos, polling /api/v1/tasks/{id}, /tasks/... download."""

    def __init__(self, task_states):
        self.headers, self.auth, self.calls = {}, None, []
        self.task_states = list(task_states)

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/api/v1/videos"):
            return FakeResp(body={"status": 200, "message": "success", "data": {"task_id": "abc"}})
        if url.endswith("/api/v1/social-metadata"):
            return FakeResp(body={"status": 200, "data": {"title": "LLM title", "caption": "cap",
                                                          "hashtags": ["#shorts"]}})
        return FakeResp(404, {"status": 404, "message": "nope"})

    def get(self, url, timeout=None, stream=False):
        self.calls.append(("GET", url, None))
        if "/api/v1/tasks/" in url:
            state = self.task_states.pop(0)
            return FakeResp(body={"status": 200, "data": state})
        if url.endswith("/tasks/abc/final-1.mp4"):
            return FakeResp(content=b"MP4DATA")
        if url.endswith("/ping"):
            return FakeResp(body="pong")
        return FakeResp(404)


DONE = {"task_id": "abc", "state": 1, "progress": 100, "videos": ["/tasks/abc/final-1.mp4"],
        "script": "the script", "terms": ["bee"]}
RUNNING = {"task_id": "abc", "state": 4, "progress": 40}


def client_for(settings, states):
    session = FakeSession(states)
    return generate.MPTClient(settings, session=session), session


def test_client_sends_auth_headers(settings, monkeypatch):
    monkeypatch.setenv("MPT_API_KEY", "k")
    monkeypatch.setenv("BASIC_AUTH_USER", "u")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "p")
    from orchestrator import config
    client, session = client_for(config.load(), [])
    assert session.headers["x-api-key"] == "k" and session.auth == ("u", "p")


def test_wait_for_task_polls_until_complete(settings):
    client, session = client_for(settings, [RUNNING, RUNNING, DONE])
    sleeps = []
    task = client.wait_for_task("abc", 5, 100, sleep=sleeps.append)
    assert task["videos"] and sleeps == [5, 5]


def test_wait_for_task_raises_on_failure_and_timeout(settings):
    failed = {"state": -1, "failed_stage": "audio", "error": "TTS timed out"}
    client, _ = client_for(settings, [failed])
    with pytest.raises(generate.GenerationError, match="audio"):
        client.wait_for_task("abc", 0, 10, sleep=lambda s: None)
    client, _ = client_for(settings, [RUNNING] * 5)
    ticks = iter(range(100))
    with pytest.raises(TimeoutError):
        client.wait_for_task("abc", 0, 2, sleep=lambda s: None, clock=lambda: next(ticks))


def test_http_error_is_reported(settings):
    client, session = client_for(settings, [])
    session.post = lambda *a, **k: FakeResp(400, {"status": 400, "message": "field required"}, method="POST")
    with pytest.raises(generate.GenerationError, match="field required"):
        client.create_video({})


def test_load_queue_merges_defaults_and_skips_disabled(settings):
    q = generate.load_queue(settings.queue_path)
    assert [r.topic_id for r in q] == ["t1", "t2"]
    assert q[0].voice_name == "en-US-JennyNeural-Female" and q[0].tags == ["shorts"]


def test_load_queue_rejects_duplicate_ids(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text("topics:\n  - {id: a, subject: x}\n  - {id: a, subject: y}\n")
    with pytest.raises(ValueError, match="duplicate"):
        generate.load_queue(p)


@pytest.fixture
def gen_settings(settings, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "x")
    monkeypatch.setenv("OLLAMA_HOST", "http://h:11434")
    from orchestrator import config
    return config.load()


def test_run_end_to_end(gen_settings):
    client, session = client_for(gen_settings, [RUNNING, DONE, DONE])
    produced = generate.run(count=2, settings=gen_settings, client=client)
    assert [p.name for p in produced] == ["t1.mp4", "t2.mp4"]
    items = {r.request.topic_id: r for _, r in review.list_items(gen_settings.output_dir, review.PENDING)}
    assert items["t1"].request.title == "LLM title"          # filled from social-metadata
    assert items["t2"].request.title == "Given title"        # queue value wins
    assert items["t1"].script == "the script"
    state = generate.load_state(gen_settings)
    assert state["t1"]["status"] == "generated"
    # second run: queue exhausted, no API calls
    session.calls.clear()
    assert generate.run(settings=gen_settings, client=client) == []
    assert session.calls == []


def test_failed_topic_is_recorded_and_retried_later(gen_settings):
    client, _ = client_for(gen_settings, [{"state": -1, "failed_stage": "llm", "error": "x"}])
    assert generate.run(count=1, settings=gen_settings, client=client) == []
    assert generate.load_state(gen_settings)["t1"]["status"] == "failed"
    assert generate.next_topics(generate.load_queue(gen_settings.queue_path),
                                generate.load_state(gen_settings), 1)[0].topic_id == "t1"


def test_dry_run_makes_no_calls_and_writes_nothing(settings, caplog):
    caplog.set_level("INFO")
    assert generate.run(dry_run=True, settings=settings) == []  # network is blocked in conftest
    assert "DRY-RUN: would POST" in caplog.text
    assert not (settings.output_dir / "queue_state.json").exists()


def test_run_without_keys_fails_loudly(settings):
    from orchestrator.config import ConfigError
    with pytest.raises(ConfigError):
        generate.run(settings=settings)
