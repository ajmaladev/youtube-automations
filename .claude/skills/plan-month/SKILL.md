---
description: Create a full month of Plot Armor Facts Shorts - pick filmable true stories, research and write them with calendar-series-writer agents, preview every footage clip, and assemble topics/calendar/YYYY-MM.json. Run as /plan-month YYYY-MM.
argument-hint: "YYYY-MM"
arguments: [month]
disable-model-invocation: true
---

# /plan-month $month

Create `topics/calendar/$month.json`: one researched 3-part series for every day of **$month**, ready for the
pipeline to render and upload, with **every footage clip previewed and matching its words**. If `$month` is
empty, use next month. Load the `calendar-writing` skill (`.claude/skills/calendar-writing/SKILL.md`) before
anything else; every step follows its rules.

## 1. Context

```bash
uv run python -m orchestrator.content_calendar titles
```

That lists every story already planned; never repeat one. Read `topics/channel.yaml` for the audience and
categories. Note the month's real anniversaries and holidays for date hooks.

## 2. Lineup

1. Draft about 8 more candidate stories than the month has days, following "Choosing stories".
2. Check each candidate's 4-6 key scenes, cached results first (free), then new searches:
   `uv run python -m orchestrator.footage cached <keyword>` and
   `uv run python -m orchestrator.footage search "scene one" "scene two" ...`
   Drop candidates whose core scenes don't return matching clip titles.
3. Pick one story per day: balance the categories (no category more than 2 days in a row), put strong
   stories early in the month, and use date hooks.
4. Write the lineup to `topics/calendar/drafts/$month/LINEUP.md` as a table (date, category, series_id, story,
   movie tie-in, leads). Show it to the user, then continue.

## 3. Write

Split the days into batches of 5-6 and start one `calendar-series-writer` agent per batch, all in the same
message so they run in parallel in the background. Run at most 4 writers at once: Pexels allows 200 searches
an hour shared by all of them. Give each agent the month and, for each day, the date, category, series_id,
story and leads from the lineup. Tell them to reuse cached terms, never run retry loops, and leave terms NOT
CHECKED rather than wait. If an agent reports that a story can't be filmed or lacks sources, choose a
replacement and send it to a new agent.

## 4. Review every draft

For each `topics/calendar/drafts/$month/*.json`:

1. `uv run python -m orchestrator.content_calendar validate <draft>` shows OK.
2. Read all 3 scripts: a hook that stops the scroll, specific cliffhangers, a quotable final line, no repeated
   phrasing across the month.
3. Spot-check the 1-2 riskiest claims per series (numbers, quotes, movie links) with WebFetch against the
   listed sources. Correct or soften anything unsupported.

## 5. Preview every clip (never skip)

1. As soon as the writers finish, start the full preview in the background:
   ```bash
   uv run python -m orchestrator.footage warm topics/calendar/drafts/$month
   ```
   It searches every uncached term, paced under the hourly limit and waiting out throttling. It prints its
   estimated time; tell the user (a free key can take a few hours for a month). Keep reviewing scripts
   (step 4) while it runs.
2. When `warm` prints `preview complete`, run
   `uv run python -m orchestrator.footage storyboard topics/calendar/drafts/$month` and read **every** pairing.
   Fix clips that don't show their words (modern scenes under old events, wrong places, repeated clip ideas,
   NO CLIP): choose better terms with `footage cached` or `footage search`, edit the drafts, then run `warm`
   and `storyboard` again.
3. Repeat until storyboard ends with `0 problem term(s), 0 not checked yet` and every pairing matches.

## 6. Assemble and check

```bash
uv run python -m orchestrator.content_calendar assemble $month
uv run python -m orchestrator.footage storyboard topics/calendar/$month.json
uv run pytest
```

All three must pass, and the storyboard must show 0 problems and 0 not checked. `assemble` uses the
Europe/London timezone for the 09:00 / 15:00 / 21:00 slots; pass `--timezone <IANA zone>` for another audience.

## 7. Report

Reply with a table (date, category, title, word counts); footage terms checked / total (must be all) and
problems (must be 0); every claim you corrected; and the test result. Don't commit or push; ask the user first.
