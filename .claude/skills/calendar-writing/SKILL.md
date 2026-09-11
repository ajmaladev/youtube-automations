---
description: Editorial rules for Plot Armor Facts content-calendar series (3-part YouTube Shorts) - story selection, facts and sources, the script formula, and Pexels footage terms that match every sentence (with a full clip preview). Load before researching, writing, reviewing or fixing anything in topics/calendar/.
user-invocable: false
---

# Writing Plot Armor Facts calendar series

Plot Armor Facts is a YouTube Shorts channel: one true story a day, told as a 3-part series (p1 morning,
p2 afternoon, p3 night). Scripts are rendered **exactly as written**: a TTS voice with karaoke subtitles over
Pexels stock footage, **one 3-second clip per video term, played in script order**. Goal: viewers who land on
any part watch to the end, want the next part, subscribe, and see pictures that match what they hear.
Facts stay 100% true.

## 1. Choosing stories

A story qualifies only if **all** of these hold:

1. **True and well documented** — enough sourced material for 3 escalating reveals and a payoff.
2. **Surprising** — a "wait, really?" core: a myth busted, an impossible survival, a hidden twist.
3. **Filmable on Pexels** — its key scenes exist as real stock video: animals, oceans, mountains, snow,
   storms, volcanoes, space, planes, ships, trains, cities, famous landmarks, old objects (radios, typewriters,
   newspapers, candles, chains). Before picking a story, check 4-6 of its key scenes with `footage cached`
   and then `footage search`, and read the titles. If the core scene can't be shown (a specific person's
   face, a rod through a skull, a microscopic animal, a comic book), pick another story.
4. **Not Marvel or DC**, no comic-book franchises, no superhero characters. Real stories behind hit movies
   (Titanic, Rocky, Jaws, Sully...) are welcome.
5. **Never repeated** — check `uv run python -m orchestrator.content_calendar titles`.
6. **YouTube-safe** — no gore, respectful about deaths, no medical advice.

Categories: `nature` (animals, oceans, weather in the wild), `adventure` (survival, rescue, exploration,
flights, space missions), `history` (people and events, real stories behind movies), `science` (space,
volcanoes, lightning, discoveries). Spread them so no category runs more than 2 days in a row. Use real date
hooks when a story's anniversary falls in the month.

## 2. Facts (non-negotiable)

- Research every series with WebSearch/WebFetch. Every name, date, number, event and cause-and-effect claim
  must be confirmed by a source you opened or saw in search results.
- `sources`: 3-5 `{title, url}` that together cover every claim: Wikipedia plus at least one non-Wikipedia
  authority (museum, university, NASA/government, Britannica, Smithsonian, major newspaper, peer-reviewed
  paper). Only URLs you confirmed exist. No fan wikis or content farms.
- Disputed or legendary details: leave out, or attribute honestly ("according to his memoir", "historians
  doubt it"). A sourced myth-bust is a great payoff.
- Movie tie-ins only when a source confirms the film is based on or depicts the story. Name the film; never
  quote films or songs.
- Never invent dialogue, thoughts, scenes or numbers. Living people: attribute findings, stay neutral.

## 3. The script formula

80-110 words **including** the fixed ending (aim 88-100, about 35 seconds):
p1 `Subscribe so you don't miss Part 2.` · p2 `Subscribe so you don't miss Part 3.` ·
p3 `Subscribe for a new true story every day.`

- **p1** HOOK → WHY CARE → SETUP → ESCALATION → SPECIFIC CLIFFHANGER → ending
- **p2** HOOK that pays off p1's cliffhanger → RECAP (`In Part 1, ...`, max 16 words) → BIGGER REVEAL →
  SPECIFIC CLIFFHANGER → ending
- **p3** HOOK teasing the payoff → RECAP (`In Part 2, ...`) → PAYOFF → FINAL LINE (max 12 words, quotable)
  → ending

Rules:
1. Hook (sentence 1), max 12 words: the most shocking true claim, a contradiction, or a movie tie-in. Never
   open with a date, "Did you know", "In this video" or a full name.
2. Open a loop in the first 2 sentences; close it later.
3. Write for the ear: 8-12 words per sentence, max 24, contractions, simple words. No parentheses,
   abbreviations, "&", "~", emojis, hashtags, markdown or non-ASCII (write Hachiko, not Hachikō).
4. Max 4 numbers per script (not counting "Part N"); round them; prefer relative time. Max 3 named people.
5. One big reveal per part, so a viewer who lands on Part 2 or 3 first still gets a satisfying moment.
6. No epilogue lists ("And in 2019... and in 2021..."); max one epilogue fact in p3.
7. Cliffhangers tease something concrete ("They are not really eels."), never "What happened next?". The next
   part's hook pays it off. The `cliffhanger` field repeats that final tease (empty for p3).
8. **Write for the footage**: every sentence should describe something a stock clip can show. Prefer "The
   sea froze solid, so no ship could reach the town" over "The situation became logistically impossible".

## 4. Footage terms (`video_terms`)

The engine plays one clip per term, in order, 3 seconds each. So:

- **Count**: `ceil(total words / 8.5)`, minimum 8, maximum 14 (a 95-word script needs 12). The validator
  enforces it.
- **Order**: term 1 plays under the first ~8 words, term 2 under the next ~8, and so on; the last term plays
  under the "Subscribe" ending, so make it a strong closing image.
- **Each term is the scene for its words**: 2-5 lowercase words, concrete subject + action + setting:
  `sled dogs running in snow`, `iceberg floating in ocean`, `vintage radio close up`, `boxer knocked down`.
- Stand-ins for things that were never filmed: a 1925 serum run becomes `husky team pulling sled`; a 1938
  radio panic becomes `family listening to vintage radio`. Famous landmarks and cities work (`eiffel tower
  paris`, `tokyo train station`); obscure place names don't. Watch for modern-looking clips under old events.
- Never character names, film titles, brands, logos, comics, cartoons, drawings, posters, gore or weapons
  aimed at people. Every term in an episode must be different; avoid reusing one term across a series' parts.
- **Pexels limits**: a free key allows **200 searches an hour**, shared by every writer. Every search is
  cached for 14 days and saved at once, so:
  - Reuse known-good terms first: `uv run python -m orchestrator.footage cached <keyword> [...]` searches the
    cache for free (e.g. `cached snow dog`, `cached ocean storm`).
  - Only `search` genuinely new phrasings, a few at a time. Never run retry loops.
  - When Pexels throttles, `search` and `storyboard` finish quickly and mark the rest NOT CHECKED. Keep
    writing and fixing what is already checked; `footage warm` finishes the preview (section 6).
  - For much faster months, ask Pexels for unlimited requests (free) at pexels.com/api.

## 5. Fields

Draft file: `topics/calendar/drafts/YYYY-MM/YYYY-MM-DD.json`, one series object. Shape: the `series`
definition in `topics/calendar/calendar.schema.json`. Look at any series in the newest
`topics/calendar/YYYY-MM.json` for a finished example.

- `series_id`: `YYYY-MM-DD-short-slug`. `date`: the day. `category`: one of the four above.
- `series_title` ≤ 45 chars, curiosity gap ("The Real Shark Attacks Behind Jaws"). `episode_title` ≤ 60 chars.
- `youtube_title`: exactly `<series_title> (Part N/3)`. `episode_id`: `<series_id>-p1|p2|p3`.
- `slot`: p1 `morning`, p2 `afternoon`, p3 `night`. `throughline`: the one question the series answers.
- `description`: one teaser sentence + one question for the comments (must contain "?"), ≤ 300 chars, no URLs.
- `tags`: 5-8 lowercase, no '#', include the film title for a tie-in.

## 6. Preview every clip (required, every time)

Nothing is finished until every clip has been previewed and matches its words:

```bash
uv run python -m orchestrator.content_calendar validate <draft or month file>
uv run python -m orchestrator.footage warm <draft, draft folder or month file>        # waits out the hourly limit
uv run python -m orchestrator.footage storyboard <draft, draft folder or month file>
```

1. `warm` searches every term that isn't cached yet, paced under the hourly limit. For a whole month it can
   take hours on a free key, so run it in the background and keep working on checked pairings meanwhile.
2. `storyboard` must end with `0 problem term(s), 0 not checked yet`.
3. Read every pairing. Fix any clip that doesn't show its words, especially modern scenes under historical
   events, wrong places, or the same clip idea repeated: pick a better term with `footage cached`, then
   `search` if needed, and run `warm` + `storyboard` again for that file.
4. Done means: validate OK, storyboard 0 problems and 0 not checked, every pairing matches, every claim has
   a source, and the hooks and cliffhangers would make you watch the next part.
