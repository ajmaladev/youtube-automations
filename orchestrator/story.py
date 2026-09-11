"""Automatic multi-part story series: researched, fact-checked and never repeated.

One series = `episodes_per_series` videos (default 3). Flow:
  1. ideate      writer LLM proposes ideas for a weighted category, told what is already covered
  2. dedupe      code drops ideas too similar to topics/history.json
  3. research    web-search model (gpt-oss + Groq browser_search) collects sourced facts; ideas without
                 `min_verified_facts` are dropped
  4. outline     writer plans the arc: throughline, beats per part (from the facts only), cliffhangers
  5. write       each part is written on its own, given the outline and the previous part's script, so
                 it recaps and picks up the cliffhanger; off-length parts are retried individually
  6. fact-check  writer checks every claim against the sourced facts and corrects or removes anything
                 unsupported (fails closed); if that leaves a part too short, a length fix follows
  7. enforce     code checks shape/length, strips model CTAs, appends "Follow for Part N." to
                 parts 1..N-1 and "Subscribe to <channel> for more videos." to the last part
  8. save        output/series/<id>.json (scripts, outline, facts, sources, fact-check report)
                 + topics/history.json

Settings live in topics/channel.yaml.

CLI: python -m orchestrator.story plan [--dry-run] [--category NAME]
     python -m orchestrator.story history
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from orchestrator import config
from orchestrator.llm import PROVIDERS, LLMClient, LLMError, ollama_base_url
from orchestrator.models import VideoRequest

log = logging.getLogger(__name__)

CHANNEL_DEFAULTS: dict[str, Any] = {
    "channel_name": "Plot Armor Facts",
    "episodes_per_series": 3,
    "words_per_episode": [90, 130],
    "ideas_per_round": 6,
    "similarity_threshold": 0.55,
    "avoid_recent": 100,
    "min_verified_facts": 8,
    "audience": "viewers who love comics, superheroes and surprising science",
    "tone": "fast, punchy, curious, a little dramatic",
    "description_footer": "Stock footage: Pexels.",
    "categories": [{"name": "comics-lore", "weight": 1, "brief": "Marvel and DC history and lore."}],
}
MAX_RESEARCHED_IDEAS = 3
MAX_WRITE_ATTEMPTS = 3
MAX_PART_ATTEMPTS = 2
STOPWORDS = set("""a an and are as at be but by for from how in into is it its of on or that the their this
to was were what when where which who why will with you your real really story stories history secret secrets
truth behind part parts facts fact explained vs fiction""".split())
CTA_RE = re.compile(r"\b(subscribe|follow (us |me )?for|stay tuned|hit the bell|smash that|like and share|"
                    r"comment below|part \d+ (is )?coming)\b", re.I)
FALLBACK_TERMS = ["city skyline at night", "dramatic storm clouds", "light rays in darkness"]


class StoryError(RuntimeError):
    pass


class LengthError(StoryError):
    """Scripts have the right shape but the wrong length - fixable without a full rewrite."""


# --- files ----------------------------------------------------------------------------

def load_channel(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else {}
    channel = {**CHANNEL_DEFAULTS, **(data or {})}
    if not channel["categories"]:
        raise StoryError(f"{path}: at least one category is required")
    return channel


def load_history(path: Path) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else []


def save_history(path: Path, history: list[dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def series_dir(settings: config.Settings) -> Path:
    # Keep series JSON under topics/ so GitHub Actions can commit day-to-day progress.
    d = Path(settings.channel_path).parent / "series"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_all_series(settings: config.Settings) -> list[dict[str, Any]]:
    items = [json.loads(p.read_text(encoding="utf-8")) for p in series_dir(settings).glob("*.json")]
    return sorted(items, key=lambda s: s.get("planned_at", ""))


# --- dedupe ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    ta = _tokens(f"{a.get('title', '')} {a.get('subject', '')}")
    tb = _tokens(f"{b.get('title', '')} {b.get('subject', '')}")
    ka = {k.lower() for k in a.get("keywords", [])} | ta
    kb = {k.lower() for k in b.get("keywords", [])} | tb
    title_ratio = SequenceMatcher(None, a.get("title", "").lower(), b.get("title", "").lower()).ratio()
    return max(_jaccard(ta, tb), _jaccard(ka, kb), title_ratio)


def find_duplicate(idea: dict[str, Any], history: list[dict[str, Any]], threshold: float) -> dict | None:
    for past in history:
        if similarity(idea, past) >= threshold:
            return past
    return None


def pick_category(channel: dict[str, Any], history: list[dict[str, Any]], rng: random.Random,
                  name: str | None = None) -> dict[str, Any]:
    cats = channel["categories"]
    if name:
        match = [c for c in cats if c["name"] == name]
        if not match:
            raise StoryError(f"unknown category {name!r}; choose from {[c['name'] for c in cats]}")
        return match[0]
    last = history[-1]["category"] if history else None
    pool = [c for c in cats if c["name"] != last] or cats
    return rng.choices(pool, weights=[float(c.get("weight", 1)) for c in pool])[0]


# --- prompts --------------------------------------------------------------------------

def _writer_system(channel: dict[str, Any]) -> str:
    return (
        f"You are the head writer of \"{channel['channel_name']}\", a YouTube Shorts channel that tells "
        f"true stories as short multi-part series. Audience: {channel['audience']}. Tone: {channel['tone']}. "
        "Accuracy matters more than drama: never invent names, dates, numbers, quotes or events. "
        "Reply with a single JSON object and nothing else."
    )


def _research_system(web: bool) -> str:
    how = ("Use web search and confirm every fact against reliable sources (official publisher sites, "
           "well-cited encyclopedias, reputable news outlets, scientific papers). "
           if web else
           "You have no web access: include only facts you are highly confident are true and widely documented. ")
    return ("You are a meticulous researcher and fact-checker for a YouTube Shorts channel. " + how +
            "Never guess. Reply with a single JSON object and nothing else.")


def _facts_block(research: dict[str, Any]) -> str:
    facts = "\n".join(f"{i}. {f['claim']}" for i, f in enumerate(research["facts"], 1))
    myths = "\n".join(f"- {m}" for m in research.get("misconceptions", [])) or "- (none listed)"
    return f"VERIFIED FACTS (the only factual material you may use):\n{facts}\n\nMYTHS (never state these as true):\n{myths}"


def _outline_text(outline: dict[str, Any]) -> str:
    lines = []
    for p in outline["parts"]:
        lines.append(f"Part {p['part']}: {p.get('title', '')}")
        lines += [f"  - {b}" for b in p["beats"]]
        if p.get("cliffhanger"):
            lines.append(f"  ends on: {p['cliffhanger']}")
    return "\n".join(lines)


def ideation_prompt(channel: dict[str, Any], category: dict[str, Any], history: list[dict[str, Any]],
                    retry: bool = False) -> tuple[str, str]:
    recent = history[-int(channel["avoid_recent"]):]
    covered = "\n".join(f"- {h['title']}: {h.get('subject', '')}" for h in recent) or "- (none yet)"
    n, eps = int(channel["ideas_per_round"]), int(channel["episodes_per_series"])
    user = f"""Propose {n} ideas for a {eps}-part Shorts series.

Category: {category['name']} - {category.get('brief', '')}

Already covered (do NOT repeat, remix or rename these; pick clearly different characters, events or angles):
{covered}

A strong idea has a surprising core question, plenty of well-documented material for {eps} escalating
reveals, and a satisfying payoff in the final part. Order ideas from strongest to weakest.
{"Your previous ideas were too close to covered series. Go for very different subjects this time." if retry else ""}
Return JSON:
{{"ideas": [{{"title": "series title, max 45 chars, no 'Part'",
             "subject": "one sentence: what the series is about",
             "hook": "why a viewer can't scroll past, max 15 words",
             "entities": ["main characters, people or things"],
             "keywords": ["3-6 lowercase topic keywords"]}}]}}"""
    return _writer_system(channel), user


def research_prompt(channel: dict[str, Any], idea: dict[str, Any], web: bool) -> tuple[str, str]:
    eps = int(channel["episodes_per_series"])
    user = f"""Research this {eps}-part Shorts series idea.

Series: {idea['title']}
About: {idea['subject']}

Collect 12-20 verified facts that together can tell a story with a setup, escalating reveals and a payoff.
Each fact is one short sentence with exact names, dates and numbers, plus the URL that confirms it{"" if web else " (empty string if you have none)"}.
Also list popular myths about this topic that are FALSE, so the writer avoids them.
Leave out anything you cannot verify.

Return JSON:
{{"facts": [{{"claim": "...", "source": "https://..."}}], "misconceptions": ["..."]}}"""
    return _research_system(web), user


def outline_prompt(channel: dict[str, Any], idea: dict[str, Any], research: dict[str, Any],
                   feedback: str = "") -> tuple[str, str]:
    eps = int(channel["episodes_per_series"])
    user = f"""Plan a {eps}-part Shorts series before it is written.

Series: {idea['title']}
About: {idea['subject']}
Hook idea: {idea.get('hook', '')}

{_facts_block(research)}

Story rules:
- Decide the throughline: the single question the whole series answers.
- Part 1 sets up the mystery. Middle parts add complications and twists. Part {eps} answers the throughline.
- Give every part 4-6 beats (one sentence each) built ONLY from the verified facts. Spread the strongest facts
  across all parts; each part must reveal something new and raise the stakes.
- Parts 1-{eps - 1} end on a specific cliffhanger (a question or tease) that the next part answers.
- The "parts" array must contain EXACTLY {eps} objects.
{feedback}
Return JSON:
{{"series_title": "max 45 chars, no 'Part'",
  "throughline": "the one question the series answers",
  "parts": [{{"part": 1, "title": "part title, max 60 chars", "beats": ["..."],
              "cliffhanger": "the tease this part ends on ('' for the last part)"}}]}}"""
    return _writer_system(channel), user


def part_prompt(channel: dict[str, Any], idea: dict[str, Any], research: dict[str, Any], outline: dict[str, Any],
                n: int, previous: str, feedback: str = "") -> tuple[str, str]:
    eps = int(channel["episodes_per_series"])
    lo, hi = channel["words_per_episode"]
    this = outline["parts"][n - 1]
    if n == 1:
        opening = "- This is the first part: after the hook, set up the mystery."
        context = "This is the first part."
    else:
        prev_cliff = outline["parts"][n - 2].get("cliffhanger", "")
        opening = (f"- Sentence 2 recaps Part {n - 1} in ONE sentence. Then continue exactly from its cliffhanger: "
                   f"\"{prev_cliff}\"")
        context = f"PART {n - 1} SCRIPT (continue from it, do not repeat it):\n{previous}"
    ending = (f"- End on this cliffhanger: \"{this.get('cliffhanger', '')}\"" if n < eps
              else "- End with a satisfying answer to the throughline.")
    user = f"""Write Part {n} of {eps} of the series "{outline['series_title'] or idea['title']}".
Throughline: {outline['throughline']}

OUTLINE:
{_outline_text(outline)}

{context}

{_facts_block(research)}

Rules for Part {n}:
- Write 9-12 short spoken sentences, {lo}-{hi} words in total. Fewer than {lo} words is rejected.
- Sentence 1 is the hook: max 12 words, a bold claim, surprising fact or burning question.
  Never start with "Did you know", "In this video", "Hey guys" or "Welcome".
{opening}
- Cover every beat of Part {n}. Explain why each fact matters; add vivid description and rhetorical
  questions to build tension (never new facts).
{ending}
- Every name, date, number and event must come from the verified facts.
- No calls to action (no subscribe/follow/like lines), no quotes from comics or films, no emojis,
  no hashtags, no stage directions, no markdown.
- video_terms: 5 real-world stock-footage search phrases (2-4 words) matching this part's mood. Never
  character names, brands, logos or anything trademarked.
- tags: 5-8 lowercase keywords without '#'.
{feedback}
Return JSON:
{{"title": "part title, max 60 chars", "hook": "the first sentence", "script": "the full narration",
  "video_terms": ["..."], "description": "1-2 sentence YouTube description", "tags": ["..."]}}"""
    return _writer_system(channel), user


def length_fix_prompt(channel: dict[str, Any], draft: dict[str, Any], research: dict[str, Any],
                      problem: str) -> tuple[str, str]:
    eps = int(channel["episodes_per_series"])
    lo, hi = channel["words_per_episode"]
    user = f"""Some parts have the wrong length: {problem}.
Fix ONLY the length: every script must be {lo}-{hi} words of spoken narration.
To lengthen, use more of the verified facts below, explain why each fact matters, and add vivid description or
rhetorical questions (never new facts). To shorten, cut filler.
Keep the hook, recap, story flow and cliffhangers. Keep the exact same JSON shape with EXACTLY {eps} episodes.
Do not add calls to action.

{_facts_block(research)}

SERIES:
{json.dumps(draft, ensure_ascii=False)}"""
    return _writer_system(channel), user


def fact_check_prompt(channel: dict[str, Any], series: dict[str, Any], research: dict[str, Any],
                      web: bool) -> tuple[str, str]:
    lo, hi = channel["words_per_episode"]
    parts = [{"part": ep["part"], "script": strip_cta(ep["script"])} for ep in series["episodes"]]
    user = f"""Fact-check this {len(parts)}-part series{" using web search and" if web else " against"} the verified facts below.
For each part, find every factual claim (names, dates, numbers, events, cause-and-effect statements).
A claim is supported only if it matches the verified facts{" or a reliable source you found" if web else ""}.
Rewrite each script so every unsupported or wrong claim is corrected or removed. Change as little as possible:
keep the hook, recap, story flow and cliffhanger.
Never make a script shorter than {lo} words: replace each removed claim with narration of similar length
(description, stakes or a rhetorical question - no new facts).

{_facts_block(research)}

SERIES:
{json.dumps(parts, ensure_ascii=False)}

Return JSON:
{{"episodes": [{{"part": 1, "unsupported": ["each claim you corrected or removed"], "script": "corrected full script"}}]}}"""
    return _research_system(web), user


# --- validation / enforcement ---------------------------------------------------------

def clean_script(text: str) -> str:
    text = re.sub(r"[\U00010000-\U0010FFFF]", "", text or "")          # emojis
    text = re.sub(r"\[[^\]]*\]|\((?:[^)]*\b(?:music|sfx|pause|beat)\b[^)]*)\)", " ", text, flags=re.I)
    text = re.sub(r"[*#_`>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_cta(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(s for s in sentences if s and not CTA_RE.search(s)).strip()


def _ending(part: int, total: int, channel: dict[str, Any]) -> str:
    if part < total:
        return f"Follow for Part {part + 1}."
    return f"Subscribe to {channel['channel_name']} for more videos."


def _terms(raw: Any, entities: list[str]) -> list[str]:
    items = [str(t).strip() for t in (raw.split(",") if isinstance(raw, str) else raw or []) if str(t).strip()]
    banned = {e for ent in entities for e in re.findall(r"[a-z0-9]+", ent.lower()) if len(e) > 3}
    items = [t for t in items if not (set(re.findall(r"[a-z0-9]+", t.lower())) & banned)]
    for extra in FALLBACK_TERMS:
        if len(items) >= 3:
            break
        items.append(extra)
    return items[:6]


def _part_no(ep: dict[str, Any]) -> int:
    try:
        return int(ep.get("part", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _word_ok(words: int, channel: dict[str, Any]) -> bool:
    lo, hi = channel["words_per_episode"]
    return lo * 0.7 <= words <= hi * 1.4


def validate_outline(outline: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
    eps = int(channel["episodes_per_series"])
    parts = outline.get("parts")
    if not isinstance(parts, list) or len(parts) != eps or not all(isinstance(p, dict) for p in parts):
        raise StoryError(f"expected {eps} parts in the outline, got {len(parts) if isinstance(parts, list) else 0}")
    parts = sorted(parts, key=_part_no)
    for i, p in enumerate(parts, start=1):
        p["part"] = i
        p["title"] = clean_script(str(p.get("title", "")))
        p["beats"] = [clean_script(str(b)) for b in p.get("beats") or [] if str(b).strip()] or [p["title"]]
        p["cliffhanger"] = clean_script(str(p.get("cliffhanger", ""))) if i < eps else ""
    return {"series_title": clean_script(str(outline.get("series_title", ""))),
            "throughline": clean_script(str(outline.get("throughline", ""))), "parts": parts}


def enforce_series(data: dict[str, Any], idea: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
    total = int(channel["episodes_per_series"])
    lo, hi = channel["words_per_episode"]
    episodes = data.get("episodes")
    if not isinstance(episodes, list) or len(episodes) != total:
        raise StoryError(f"expected {total} episodes, got {len(episodes) if isinstance(episodes, list) else 0}")
    out, problems = [], []
    for part, ep in enumerate(sorted(episodes, key=_part_no), start=1):
        body = strip_cta(clean_script(str(ep.get("script", ""))))
        hook = clean_script(str(ep.get("hook", "")))
        if hook and hook.lower()[:40] not in body.lower()[:200]:
            body = f"{hook} {body}"
        words = len(body.split())
        if not _word_ok(words, channel):
            problems.append(f"part {part} has {words} words")
        out.append({
            "part": part,
            "title": clean_script(str(ep.get("title", "")))[:60] or f"Part {part}",
            "hook": hook,
            "script": f"{body} {_ending(part, total, channel)}",
            "cliffhanger": clean_script(str(ep.get("cliffhanger", ""))),
            "video_terms": _terms(ep.get("video_terms"), idea.get("entities", [])),
            "description": clean_script(str(ep.get("description", "")))[:400],
            "tags": [str(t).lstrip("#").strip().lower() for t in ep.get("tags", []) if str(t).strip()][:8],
        })
    if problems:
        raise LengthError(f"{', '.join(problems)} (target {lo}-{hi})")
    return {
        "series_title": clean_script(str(data.get("series_title") or idea["title"]))[:80],
        "throughline": clean_script(str(data.get("throughline", ""))),
        "episodes": out,
    }


# --- pipeline steps ---------------------------------------------------------------------

def research(researcher: LLMClient, channel: dict[str, Any], idea: dict[str, Any], web: bool) -> dict | None:
    reply = researcher.chat_json(*research_prompt(channel, idea, web), temperature=0.2)
    facts = []
    for item in reply.get("facts") or []:
        claim, source = ((str(item.get("claim", "")), str(item.get("source", ""))) if isinstance(item, dict)
                         else (str(item), ""))
        claim, source = claim.strip(), source.strip()
        if claim and (source.startswith("http") or not web):
            facts.append({"claim": claim, "source": source})
    if len(facts) < int(channel["min_verified_facts"]):
        log.info("dropping idea %r: only %d verified facts", idea["title"], len(facts))
        return None
    return {"facts": facts[:25], "misconceptions": [str(m) for m in reply.get("misconceptions") or []][:10]}


def write_part(llm: LLMClient, channel: dict[str, Any], idea: dict[str, Any], facts: dict[str, Any],
               outline: dict[str, Any], n: int, previous: str) -> dict[str, Any]:
    lo, hi = channel["words_per_episode"]
    feedback, words = "", 0
    for _ in range(MAX_PART_ATTEMPTS):
        ep = llm.chat_json(*part_prompt(channel, idea, facts, outline, n, previous, feedback), temperature=0.8)
        body = strip_cta(clean_script(str(ep.get("script", ""))))
        words = len(body.split())
        if _word_ok(words, channel):
            return ep
        advice = ("add 3-4 more sentences that explain why the facts matter and build tension" if words < lo
                  else "cut filler sentences")
        feedback = f"\nYour last version had {words} words. It MUST be {lo}-{hi} words: {advice}."
        log.warning("part %d had %d words (target %d-%d); rewriting that part", n, words, lo, hi)
    raise LengthError(f"part {n} has {words} words (target {lo}-{hi})")


def fact_check(checker: LLMClient, channel: dict[str, Any], series: dict[str, Any], idea: dict[str, Any],
               facts: dict[str, Any], web: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reply = checker.chat_json(*fact_check_prompt(channel, series, facts, web), temperature=0.2)
    fixed = {_part_no(ep): ep for ep in reply.get("episodes") or [] if isinstance(ep, dict)}
    total = len(series["episodes"])
    if sorted(fixed) != list(range(1, total + 1)):
        raise StoryError(f"fact-check returned parts {sorted(fixed)} instead of 1-{total}")
    merged = {**series, "episodes": [
        # hook cleared so a corrected opening line is never re-prepended from the draft
        {**ep, "hook": "", "script": str(fixed[ep["part"]].get("script") or ep["script"])}
        for ep in series["episodes"]]}
    try:
        checked = enforce_series(merged, idea, channel)
    except LengthError as exc:
        log.warning("fact-check left scripts off-length (%s); asking for a length fix", exc)
        reply = checker.chat_json(*length_fix_prompt(channel, merged, facts, str(exc)), temperature=0.6)
        by_part = {_part_no(ep): ep for ep in reply.get("episodes") or [] if isinstance(ep, dict)}
        checked = enforce_series({**merged, "episodes": [
            {**ep, "script": str(by_part.get(ep["part"], {}).get("script") or ep["script"])}
            for ep in merged["episodes"]]}, idea, channel)
    for ep, orig in zip(checked["episodes"], series["episodes"]):
        ep["hook"] = orig["hook"]
    report = [{"part": p, "unsupported": [str(c) for c in fixed[p].get("unsupported") or []]}
              for p in range(1, total + 1)]
    for r in report:
        if r["unsupported"]:
            log.info("fact-check corrected part %d: %s", r["part"], "; ".join(r["unsupported"]))
    return checked, report


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "series"


def plan_series(llm: LLMClient, channel: dict[str, Any], history: list[dict[str, Any]],
                category: dict[str, Any], research_llm: LLMClient | None = None) -> dict[str, Any]:
    threshold = float(channel["similarity_threshold"])
    web = research_llm is not None
    researcher = research_llm or llm

    chosen, researched = None, 0
    for round_no in range(2):
        system, user = ideation_prompt(channel, category, history, retry=round_no > 0)
        for cand in llm.chat_json(system, user, temperature=0.9 + 0.1 * round_no).get("ideas") or []:
            if not isinstance(cand, dict) or not cand.get("title") or not cand.get("subject"):
                continue
            dup = find_duplicate(cand, history, threshold)
            if dup:
                log.info("skipping idea %r (too close to %r)", cand["title"], dup["title"])
                continue
            if researched >= MAX_RESEARCHED_IDEAS:
                break
            researched += 1
            facts = research(researcher, channel, cand, web)
            if facts:
                chosen = (cand, facts)
                break
        if chosen or researched >= MAX_RESEARCHED_IDEAS:
            break
    if not chosen:
        raise StoryError("every idea was too similar to past series or lacked verified facts; "
                         "widen categories in topics/channel.yaml")
    idea, facts = chosen
    log.info("chosen series idea [%s]: %s (%d verified facts)", category["name"], idea["title"], len(facts["facts"]))

    feedback, last_error = "", None
    for _ in range(MAX_WRITE_ATTEMPTS):
        try:
            outline = validate_outline(llm.chat_json(*outline_prompt(channel, idea, facts, feedback),
                                                     temperature=0.8), channel)
            episodes, previous = [], ""
            for planned in outline["parts"]:
                ep = write_part(llm, channel, idea, facts, outline, planned["part"], previous)
                previous = strip_cta(clean_script(str(ep.get("script", ""))))
                episodes.append({**ep, "part": planned["part"], "title": ep.get("title") or planned["title"],
                                 "cliffhanger": planned["cliffhanger"]})
            series = enforce_series({"series_title": outline["series_title"] or idea["title"],
                                     "throughline": outline["throughline"], "episodes": episodes}, idea, channel)
            series, report = fact_check(llm, channel, series, idea, facts, web=False)
            break
        except StoryError as exc:
            last_error = exc
            feedback = f"\nYour previous attempt was rejected: {exc}. Fix that this time."
            log.warning("draft rejected (%s); rewriting", exc)
    else:
        raise StoryError(f"could not write a valid, fact-checked series: {last_error}")

    planned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "series_id": f"{_slug(series['series_title'])}-{planned_at[:10].replace('-', '')}",
        "series_title": series["series_title"],
        "throughline": series["throughline"],
        "subject": idea["subject"],
        "category": category["name"],
        "entities": idea.get("entities", []),
        "keywords": idea.get("keywords", []),
        "planned_at": planned_at,
        "writer": f"{llm.provider}/{llm.model}",
        "researcher": f"{researcher.provider}/{researcher.model}" + ("" if web else " (no web)"),
        "fact_checker": f"{llm.provider}/{llm.model} (against sourced facts)",
        "outline": outline["parts"],
        "facts": facts["facts"],
        "misconceptions": facts["misconceptions"],
        "fact_check": report,
        "episodes": series["episodes"],
    }


def episode_requests(series: dict[str, Any], defaults: dict[str, Any], channel: dict[str, Any]) -> list[VideoRequest]:
    total = len(series["episodes"])
    base = {k: v for k, v in defaults.items() if k not in {"video_script_prompt", "title", "description"}}
    sources = list(dict.fromkeys(f["source"] for f in series.get("facts", [])
                                 if str(f.get("source", "")).startswith("http")))[:3]
    sources_block = ("\n\nSources:\n" + "\n".join(sources)) if sources else ""
    out = []
    for ep in series["episodes"]:
        part = ep["part"]
        description = (f"{ep['title']}. {ep['description']}\n\n"
                       f"Part {part} of {total}: {series['series_title']}"
                       f"{sources_block}\n\n{channel['description_footer']}").strip()
        tags = list(dict.fromkeys([*base.get("tags", []), *ep["tags"], series["category"]]))
        out.append(VideoRequest.from_dict({
            **base,
            "topic_id": f"{series['series_id']}-p{part}",
            "subject": f"{series['series_title']} - part {part}: {ep['title']}",
            "title": f"{series['series_title']} (Part {part}/{total})",
            "description": description,
            "tags": tags,
            "video_script": ep["script"],
            "video_terms": ", ".join(ep["video_terms"]),
        }))
    return out


def all_episode_requests(settings: config.Settings, defaults: dict[str, Any]) -> list[VideoRequest]:
    channel = load_channel(settings.channel_path)
    return [r for s in load_all_series(settings) for r in episode_requests(s, defaults, channel)]


def pending_episodes(settings: config.Settings, state: dict[str, Any], defaults: dict[str, Any]) -> list[VideoRequest]:
    return [r for r in all_episode_requests(settings, defaults)
            if state.get(r.topic_id, {}).get("status") != "generated"]


def make_llm(settings: config.Settings) -> LLMClient:
    provider = settings.story_llm_provider
    if provider not in PROVIDERS:
        raise StoryError(f"STORY_LLM_PROVIDER must be one of {sorted(PROVIDERS)}")
    base, model = PROVIDERS[provider]
    if provider == "ollama":
        base = ollama_base_url(settings.ollama_host)
    keys = {"groq": settings.groq_api_key, "gemini": settings.gemini_api_key, "openai": settings.openai_api_key}
    return LLMClient(provider=provider, model=settings.story_llm_model or model,
                     base_url=settings.story_llm_base_url or base, api_key=keys.get(provider, ""))


def make_research_llm(settings: config.Settings) -> LLMClient | None:
    """Web-search researcher on Groq. None -> writer model researches without web.

    gpt-oss models get Groq's built-in browser_search tool; groq/compound* models search on their own.
    """
    model = settings.story_research_model
    if settings.story_llm_provider != "groq" or not model or model.lower() == "off":
        return None
    extra = ({"tools": [{"type": "browser_search"}], "reasoning_effort": "low"}
             if model.startswith("openai/gpt-oss") else {})
    return LLMClient(provider="groq", model=model, base_url=settings.story_llm_base_url or PROVIDERS["groq"][0],
                     api_key=settings.groq_api_key, json_mode=False, extra_body=extra)


def plan_and_save(settings: config.Settings, defaults: dict[str, Any], dry_run: bool = False,
                  llm: LLMClient | None = None, research_llm: LLMClient | None = None,
                  rng: random.Random | None = None, category: str | None = None) -> list[VideoRequest]:
    channel = load_channel(settings.channel_path)
    history = load_history(settings.history_path)
    cat = pick_category(channel, history, rng or random.Random(), category)
    if dry_run:
        log.info("DRY-RUN: would ask %s for a %d-part series in category %r, avoiding %d past series "
                 "(research: %s)", settings.story_llm_provider, int(channel["episodes_per_series"]),
                 cat["name"], len(history), settings.story_research_model if settings.story_llm_provider == "groq"
                 else "writer model, no web")
        return []
    settings.require("story")
    if llm is None:
        llm = make_llm(settings)
        research_llm = research_llm or make_research_llm(settings)
        if research_llm is None and not settings.story_allow_unverified:
            raise StoryError(
                "no web research available: set STORY_LLM_PROVIDER=groq (with STORY_RESEARCH_MODEL). Without it "
                "the model invents its own 'facts'. STORY_ALLOW_UNVERIFIED=true is for testing only.")
    series = plan_series(llm, channel, history, cat, research_llm)
    path = series_dir(settings) / f"{series['series_id']}.json"
    n = 2
    while path.exists():
        series["series_id"] = f"{series['series_id'].rsplit('-v', 1)[0]}-v{n}"
        path, n = series_dir(settings) / f"{series['series_id']}.json", n + 1
    path.write_text(json.dumps(series, indent=2, ensure_ascii=False), encoding="utf-8")
    history.append({k: series[k] for k in ("series_id", "category", "entities", "keywords", "planned_at")}
                   | {"title": series["series_title"], "subject": series["subject"]})
    save_history(settings.history_path, history)
    log.info("planned series %s (%d episodes) -> %s", series["series_id"], len(series["episodes"]), path)
    return episode_requests(series, defaults, channel)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.story", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", default="plan", choices=["plan", "history"])
    p.add_argument("--dry-run", action="store_true", help="log what would be asked; no LLM call")
    p.add_argument("--category", help="force a category from topics/channel.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    config.setup_logging(args.verbose)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # scripts contain curly quotes etc.
    settings = config.load()
    if args.action == "history":
        for h in load_history(settings.history_path):
            print(f"{h['planned_at'][:10]}  [{h['category']}]  {h['title']}")
        return 0
    from orchestrator.generate import load_defaults
    for req in plan_and_save(settings, load_defaults(settings.queue_path), dry_run=args.dry_run,
                             category=args.category):
        words = len(req.extra_params["video_script"].split())
        print(f"{req.topic_id}: {req.title} ({words} words)\n  {req.extra_params['video_script']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
