import json
import random

import pytest

from orchestrator import config, generate, story
from orchestrator.config import REPO_ROOT
from orchestrator.llm import LLMClient, LLMFatalError, ollama_base_url, parse_duration, parse_json_reply

CHANNEL = story.load_channel(REPO_ROOT / "topics" / "channel.yaml")

PAST = {"series_id": "venom-1", "title": "Venom's Secret Origin", "subject": "How the Venom symbiote was created",
        "category": "comics-lore", "entities": ["Venom"], "keywords": ["venom", "symbiote"],
        "planned_at": "2026-09-01T00:00:00+00:00"}
IDEA_DUP = {"title": "The Secret Origin of Venom", "subject": "How the Venom symbiote was created",
            "entities": ["Venom"], "keywords": ["venom", "symbiote"]}
IDEA_NEW = {"title": "Why Superman Almost Died in 1992", "subject": "The Death of Superman event",
            "entities": ["Superman", "Doomsday"], "keywords": ["superman", "doomsday"]}
IDEA_NEW2 = {"title": "How Spider Silk Beats Steel", "subject": "Why spider silk is tougher than steel by weight",
             "entities": ["spiders"], "keywords": ["spider", "silk"]}
TERMS = ["superman statue", "city skyline", "storm clouds", "printing press"]


def ideas(*items):
    return {"ideas": list(items)}


def facts(n=10):
    return {"facts": [{"claim": f"Verified fact {i}", "source": f"https://example.com/{i}"} for i in range(n)],
            "misconceptions": ["A popular myth"]}


def script(i, n_words=100, word="lore", cta=False):
    return f"Hook line number {i}. {' '.join([word] * n_words)}." + (" Subscribe to us for more!" if cta else "")


def outline(parts=3):
    return {"series_title": "Why Superman Almost Died", "throughline": "Why did DC kill Superman?",
            "parts": [{"part": i, "title": f"Ep {i}", "beats": [f"beat {i}a", f"beat {i}b"],
                       "cliffhanger": f"tease {i}"} for i in range(1, parts + 1)]}


def part(i, n_words=100, cta=False):
    return {"title": f"Ep {i}", "hook": f"Hook line number {i}.", "script": script(i, n_words, cta=cta),
            "video_terms": TERMS, "description": "desc", "tags": ["#Comics", "dc"]}


def parts():
    return [part(1), part(2), part(3)]


def draft(parts=3, n_words=120, cta=False):
    return {"series_title": "Why Superman Almost Died", "throughline": "Why did DC kill Superman?",
            "episodes": [{**part(i, n_words, cta), "part": i, "cliffhanger": f"tease {i}"} for i in range(1, parts + 1)]}


def fact_checked(parts=3, fix_part=1, n_words=100):
    return {"episodes": [{"part": i, "unsupported": ["made-up sales number"] if i == fix_part else [],
                          "script": script(i, n_words, "saga" if i == fix_part else "lore")}
                         for i in range(1, parts + 1)]}


class FakeLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def chat_json(self, system, user, temperature=0.8):
        self.prompts.append(user)
        return self.replies.pop(0)


class FakeEngine:
    def __init__(self, fail_on=()):
        self.payloads, self.fail_on = [], set(fail_on)

    def create_video(self, payload):
        self.payloads.append(payload)
        if len(self.payloads) in self.fail_on:
            raise generate.GenerationError("boom")
        return f"task{len(self.payloads)}"

    def wait_for_task(self, task_id, *a, **k):
        return {"state": 1, "videos": [f"/tasks/{task_id}/final-1.mp4"], "script": "s", "terms": []}

    def download(self, uri, dest):
        dest.write_bytes(uri.encode())
        return dest


@pytest.fixture
def story_settings(settings, monkeypatch, tmp_path):
    queue = tmp_path / "empty_queue.yaml"
    queue.write_text("defaults:\n  voice_name: en-US-AndrewMultilingualNeural-Male\n  tags: [shorts]\n"
                     "  video_script_prompt: old manual prompt\ntopics: []\n", encoding="utf-8")
    history = tmp_path / "history.json"
    history.write_text(json.dumps([PAST]), encoding="utf-8")
    channel = tmp_path / "channel.yaml"
    channel.write_text((REPO_ROOT / "topics" / "channel.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "series").mkdir(exist_ok=True)
    for k, v in {
        "TOPIC_QUEUE_PATH": str(queue),
        "STORY_HISTORY_PATH": str(history),
        "CHANNEL_CONFIG_PATH": str(channel),
        "AUTO_SERIES": "true",
        "PEXELS_API_KEY": "x",
        "OLLAMA_HOST": "http://h:11434",
    }.items():
        monkeypatch.setenv(k, v)
    return config.load()


def plan(settings, replies):
    llm = FakeLLM(replies)
    reqs = story.plan_and_save(settings, generate.load_defaults(settings.queue_path), llm=llm, rng=random.Random(1))
    return reqs, llm


def saved_series(settings):
    return [json.loads(p.read_text(encoding="utf-8")) for p in story.series_dir(settings).glob("*.json")]


def test_similarity_flags_same_story_but_allows_new_one():
    assert story.similarity(IDEA_DUP, PAST) >= CHANNEL["similarity_threshold"]
    assert story.find_duplicate(IDEA_NEW, [PAST], CHANNEL["similarity_threshold"]) is None


def test_full_plan_research_outline_parts_fact_check(story_settings):
    replies = [ideas(IDEA_DUP, IDEA_NEW), facts(), outline(), part(1, cta=True), part(2), part(3), fact_checked()]
    reqs, llm = plan(story_settings, replies)
    ideate, research, plan_outline, p1, p2, p3, check = llm.prompts
    assert "Venom's Secret Origin" in ideate                        # told what is already covered
    assert IDEA_NEW["title"] in research and IDEA_DUP["title"] not in research
    assert "Verified fact 0" in plan_outline and "EXACTLY 3" in plan_outline
    assert "This is the first part" in p1 and 'End on this cliffhanger: "tease 1"' in p1
    assert "PART 1 SCRIPT" in p2 and "Hook line number 1." in p2 and 'continue exactly from its cliffhanger: "tease 1"' in p2
    assert "satisfying answer to the throughline" in p3
    assert "Verified fact 0" in check and "Never make a script shorter" in check

    scripts = [r.extra_params["video_script"] for r in reqs]
    assert scripts[0].startswith("Hook line number 1.") and "saga" in scripts[0]   # fact-check correction applied
    assert scripts[0].endswith("Follow for Part 2.") and scripts[1].endswith("Follow for Part 3.")
    assert scripts[2].endswith("Subscribe to Plot Armor Facts for more videos.")
    assert sum("Subscribe" in s for s in scripts) == 1
    assert [r.title for r in reqs] == [f"Why Superman Almost Died (Part {i}/3)" for i in (1, 2, 3)]
    assert "Sources:\nhttps://example.com/0" in reqs[0].description
    assert "superman" not in reqs[0].extra_params["video_terms"].lower()
    assert "video_script_prompt" not in reqs[0].extra_params

    series = saved_series(story_settings)[0]
    assert series["fact_check"][0]["unsupported"] == ["made-up sales number"]
    assert series["throughline"] == "Why did DC kill Superman?" and len(series["facts"]) == 10
    assert [p["cliffhanger"] for p in series["outline"]] == ["tease 1", "tease 2", ""]
    assert [h["title"] for h in story.load_history(story_settings.history_path)] == [
        "Venom's Secret Origin", "Why Superman Almost Died"]


def test_idea_without_enough_verified_facts_is_dropped(story_settings):
    plan(story_settings, [ideas(IDEA_NEW, IDEA_NEW2), facts(n=3), facts(), outline(), *parts(), fact_checked()])
    assert story.load_history(story_settings.history_path)[-1]["subject"] == IDEA_NEW2["subject"]


def test_bad_outline_is_rewritten_with_feedback(story_settings):
    reqs, llm = plan(story_settings, [ideas(IDEA_NEW), facts(), outline(parts=1), outline(), *parts(), fact_checked()])
    assert "previous attempt was rejected: expected 3 parts" in llm.prompts[3]
    assert len(reqs) == 3


def test_short_part_is_rewritten_on_its_own(story_settings):
    replies = [ideas(IDEA_NEW), facts(), outline(), part(1, n_words=40), part(1), part(2), part(3), fact_checked()]
    reqs, llm = plan(story_settings, replies)
    assert "Your last version had 44 words" in llm.prompts[4] and "Write Part 1 of 3" in llm.prompts[4]
    assert len(llm.prompts) == 8 and len(reqs) == 3


def test_fact_check_that_shortens_scripts_gets_a_length_fix(story_settings):
    replies = [ideas(IDEA_NEW), facts(), outline(), *parts(), fact_checked(n_words=30), draft()]
    reqs, llm = plan(story_settings, replies)
    assert "wrong length" in llm.prompts[-1] and "part 1 has 34 words" in llm.prompts[-1]
    assert all(len(r.extra_params["video_script"].split()) > 100 for r in reqs)


def test_failed_fact_check_saves_nothing(story_settings):
    bad = {"episodes": []}
    replies = [ideas(IDEA_NEW), facts()] + [outline(), *parts(), bad] * 3
    with pytest.raises(story.StoryError, match="fact-checked"):
        plan(story_settings, replies)
    assert len(story.load_history(story_settings.history_path)) == 1
    assert saved_series(story_settings) == []


def test_enforce_strips_model_cta_and_validates_shape():
    series = story.enforce_series(draft(cta=True), IDEA_NEW, CHANNEL)
    assert "Subscribe to us" not in series["episodes"][0]["script"]
    assert series["episodes"][0]["tags"][0] == "comics"
    with pytest.raises(story.LengthError, match="part 1 has 24 words, part 2 has 24 words"):
        story.enforce_series(draft(n_words=20), IDEA_NEW, CHANNEL)
    with pytest.raises(story.StoryError, match="episodes"):
        story.enforce_series(draft(parts=2), IDEA_NEW, CHANNEL)


def test_all_duplicate_ideas_raise_and_history_is_untouched(story_settings):
    with pytest.raises(story.StoryError, match="too similar"):
        plan(story_settings, [ideas(IDEA_DUP), ideas(IDEA_DUP)])
    assert len(story.load_history(story_settings.history_path)) == 1


def test_generate_plans_series_and_resumes_failed_episode(story_settings):
    engine = FakeEngine(fail_on={2})
    llm = FakeLLM([ideas(IDEA_NEW), facts(), outline(), *parts(), fact_checked(fix_part=0)])
    produced = generate.run(count=3, settings=story_settings, client=engine, llm=llm)
    assert [p.name.rsplit("-", 1)[1] for p in produced] == ["p1.mp4", "p3.mp4"]
    assert engine.payloads[0]["video_script"].endswith("Follow for Part 2.")
    series_id = produced[0].name.rsplit("-p", 1)[0]

    idle_llm = FakeLLM([])
    again = generate.run(count=1, settings=story_settings, client=FakeEngine(), llm=idle_llm)
    assert [p.name for p in again] == [f"{series_id}-p2.mp4"]
    assert idle_llm.prompts == []  # resumed without planning a new series


def test_manual_queue_topics_run_before_series(settings, monkeypatch):
    monkeypatch.setenv("AUTO_SERIES", "true")
    llm = FakeLLM([])
    todo = generate.select_topics(config.load(), generate.load_queue(settings.queue_path), {}, {}, 2, llm=llm)
    assert [r.topic_id for r in todo] == ["t1", "t2"] and llm.prompts == []


def test_real_planning_refuses_without_web_research(story_settings):
    with pytest.raises(story.StoryError, match="no web research"):
        story.plan_and_save(story_settings, {})  # provider ollama -> no browser-search researcher
    assert len(story.load_history(story_settings.history_path)) == 1


def test_dry_run_plans_nothing(story_settings, caplog):
    caplog.set_level("INFO")
    assert generate.run(dry_run=True, settings=story_settings) == []
    assert "DRY-RUN: would ask ollama for a 3-part series" in caplog.text
    assert len(story.load_history(story_settings.history_path)) == 1


def test_provider_wiring(monkeypatch):
    monkeypatch.setenv("STORY_LLM_PROVIDER", "groq")
    assert config.load().missing_for("story") == ["GROQ_API_KEY"]
    monkeypatch.setenv("GROQ_API_KEY", "g")
    s = config.load()
    writer, researcher = story.make_llm(s), story.make_research_llm(s)
    assert (writer.base_url, writer.model, writer.api_key) == ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b", "g")
    assert writer.extra_body == {"reasoning_effort": "low"} and writer.json_mode
    assert (researcher.model, researcher.json_mode) == ("openai/gpt-oss-120b", False)
    assert researcher.extra_body == {"tools": [{"type": "browser_search"}], "reasoning_effort": "low"}
    monkeypatch.setenv("STORY_RESEARCH_MODEL", "off")
    assert story.make_research_llm(config.load()) is None
    monkeypatch.setenv("STORY_LLM_PROVIDER", "ollama")
    monkeypatch.delenv("STORY_RESEARCH_MODEL")
    assert story.make_research_llm(config.load()) is None


# --- llm client ---------------------------------------------------------------------------

class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body, headers or {}
        self.text = json.dumps(body) if body else "error"

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.responses.pop(0)


OK = Resp(200, {"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]})


def test_llm_client_waits_for_rate_limit_reset_and_parses_fenced_json():
    session, sleeps = Session([Resp(429, headers={"retry-after": "7"}), Resp(503), OK]), []
    client = LLMClient("groq", "m", "https://x/v1", api_key="k", session=session, sleep=sleeps.append)
    assert client.chat_json("sys", "user") == {"a": 1}
    assert sleeps == [8.0, 4]
    url, body, headers = session.calls[-1]
    assert url == "https://x/v1/chat/completions" and headers == {"Authorization": "Bearer k"}
    assert body["response_format"] == {"type": "json_object"}


def test_llm_client_options():
    session = Session([OK])
    LLMClient("groq", "groq/compound", "https://x/v1", json_mode=False, session=session).chat_json("s", "u")
    assert "response_format" not in session.calls[0][1]
    assert LLMClient("groq", "openai/gpt-oss-120b", "u").extra_body == {"reasoning_effort": "low"}
    assert session.calls[0][1]["max_completion_tokens"] == 4096


def test_llm_client_falls_back_when_json_mode_validation_fails():
    rejected = Resp(400, {"error": {"code": "json_validate_failed", "failed_generation": ""}})
    session = Session([rejected, OK])
    client = LLMClient("groq", "m", "https://x/v1", session=session, sleep=lambda s: None)
    assert client.chat_json("s", "u") == {"a": 1}
    assert len(session.calls) == 2 and "response_format" not in session.calls[-1][1]


def test_llm_client_fails_fast_when_daily_token_budget_is_spent():
    tpd = Resp(429, {"error": {"message": "Rate limit reached ... on tokens per day (TPD): Limit 200000"}})
    session = Session([tpd, OK])
    with pytest.raises(LLMFatalError, match="tokens per day"):
        LLMClient("groq", "m", "https://x/v1", session=session, sleep=lambda s: None).chat_json("s", "u")
    assert len(session.calls) == 1


def test_llm_client_does_not_retry_auth_errors():
    session = Session([Resp(401)])
    with pytest.raises(LLMFatalError):
        LLMClient("groq", "m", "https://x/v1", session=session, sleep=lambda s: None).chat_json("s", "u")
    assert len(session.calls) == 1


def test_helpers():
    assert ollama_base_url("http://host.docker.internal:11434") == "http://127.0.0.1:11434/v1"
    assert parse_json_reply('Sure! {"ideas": []} Hope this helps') == {"ideas": []}
    assert parse_duration("1m2.5s") == 62.5 and parse_duration("250ms") == 0.25 and parse_duration("x") is None
