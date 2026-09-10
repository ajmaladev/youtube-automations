# youtube-automations

## Pipeline: MoneyPrinterTurbo → human review → YouTube

```
topics/queue.yaml ──► orchestrator.generate ──REST──► MoneyPrinterTurbo (Docker, behind Caddy basic auth)
                              │
                              ▼
                     output/pending/<id>.mp4 + <id>.json
                              │   you: `make review ARGS="approve <id>"` (or move the pair by hand)
                              ▼
                     output/approved/ ──► orchestrator.upload ──► YouTube Data API v3 (private, AI-disclosed)
                              │
                              ▼
                     output/uploaded/  (sidecar gets youtube_video_id)
```

| Path | What |
|---|---|
| `vendor/moneyprinterturbo/` | Upstream MPT, **unmodified** except `resource/songs/` emptied. Gitignored; rebuild with `make vendor` (`MPT_REF=<tag>` to pin). Consumed only via REST. |
| `orchestrator/` | `config` (env + validation), `models`, `generate` (MPT client), `review` (approval gate), `upload` (OAuth, resumable upload, quota), `scheduler` (daily entrypoint), `render_config` (fills `${VAR}` placeholders at container start). |
| `config/config.template.toml` | Tracked copy of MPT's `config.example.toml` with `${VAR}` placeholders and the free stack (Ollama / Edge TTS / Pexels / Edge subtitles). `make setup` copies it to gitignored `config/config.toml`, which is mounted read-only and rendered **inside** the container, so secrets live only in `.env`. |
| `config/env.example` | Every env var, with where to get it and whether it's optional. `make setup` copies it to `.env`. |
| `deploy/Caddyfile` | Basic auth for the API (`:8080`) and WebUI (`:8501`). MPT ships no WebUI auth, and the WebUI shows your API keys. |
| `topics/queue.yaml` | Hand-edited topic list. The pipeline never rewrites it; progress lives in `output/queue_state.json`. |

### Commands

Every entrypoint accepts `--dry-run`, which logs what would happen and calls no API. It works with no credentials.

```bash
make vendor                      # clone MPT (no .git), strip bundled songs
make setup                       # uv sync, output dirs, config/config.toml, .env
make test                        # offline; all network calls mocked/blocked
make daily ARGS=--dry-run        # full run, nothing executed
make up                          # MPT API + WebUI + Caddy (needs BASIC_AUTH_* in .env)
make generate                    # next DAILY_GENERATE_COUNT topics -> output/pending/
make review                      # list; ARGS="approve <id>" / "reject <id>" / "show <id>"
make auth                        # one-time Google OAuth consent -> .credentials/youtube.token.json
make upload                      # upload output/approved/ within quota
make daily                       # generate + upload (cron / Task Scheduler), or ARGS="--at 09:00"
```

On Windows, `make` isn't installed by default. Use `winget install ezwinports.make` (run from Git Bash), WSL, or run the underlying `uv run python -m orchestrator.<module>` commands directly.

### MoneyPrinterTurbo API contract (read from vendored source)

All routes are under `/api/v1` (`app/controllers/v1/base.py`). If `[app].api_key` is non-empty, every `/api/v1/*` and `/tasks/*` request needs an `x-api-key` header (`app/controllers/base.py::verify_token`). Swagger is at `/docs`, and health is `GET /ping` → `"pong"`.

**Create: `POST /api/v1/videos`**. The body is `TaskVideoRequest` (= `VideoParams`, `app/models/schema.py`). Only `video_subject` is required. Fields the orchestrator sends or relies on:

| field | type / default | notes |
|---|---|---|
| `video_subject` | str, **required** | topic; the LLM writes the script from it |
| `video_script` | str `""` | supply your own script to skip the LLM |
| `video_terms` | str \| list \| null | stock-footage search terms; LLM-generated if null |
| `video_aspect` | `"9:16"` (default) \| `"16:9"` \| `"1:1"` | |
| `video_source` | str `"pexels"` | `pexels`, `pixabay`, `coverr`, `local`, paid generators… |
| `video_clip_duration` | int `5` (≥1) | |
| `video_count` | int `1` (≥1) | |
| `video_language` | str `""` | auto-detect if empty |
| `voice_name` | str `""` | **the TTS provider is inferred from the voice name**; the API has no `tts_server` field. Edge TTS voices look like `en-US-JennyNeural-Female` (`voice.py::is_azure_v1_voice`) |
| `voice_rate` / `voice_volume` | float `1.0` | |
| `bgm_type` / `bgm_file` / `bgm_volume` | `"random"` / `""` / `0.2` | `bgm_type=""` means no music; `random` with an empty songs dir also falls back to none (`video.py::get_bgm_file`) |
| `subtitle_enabled` | bool `true` | |
| `subtitle_position` | str (from `[ui]`, default `"bottom"`) | `top`/`center`/`bottom`/`custom` |
| `font_name`, `font_size` (60), `text_fore_color`, `stroke_color`, `stroke_width` (1.5) | | subtitle styling |
| `paragraph_number` | int `1` (1–10) | script length |
| `video_script_prompt` / `custom_system_prompt` | str, ≤2000 / ≤8000 | |

Response `200` (filtered through `response_model=TaskResponse`):
```json
{"status": 200, "message": "success", "data": {"task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558"}}
```
Errors use the same envelope: `400` for validation (`"message": "field required"`, `data` = pydantic errors), `401` for an invalid API key, and `429` when the task queue is full (`max_queued_tasks`).

**Poll: `GET /api/v1/tasks/{task_id}`** → `{"status":200,"data":{…TaskStatusData…}}`
- `state`: `4` processing, `1` complete, `-1` failed (`app/models/const.py`)
- `progress`: 0–100
- on completion: `videos` (e.g. `["/tasks/<task_id>/final-1.mp4"]`, absolute if `[app].endpoint` is set), `combined_videos`, `script`, `terms`, `audio_file`, `audio_duration`, `subtitle_path`, `materials`, `warnings`
- on failure: `failed_stage`, `error`
- `404` if unknown. Task state is in memory unless `enable_redis = true`, so an API restart forgets tasks.

**Download:** `GET <videos[0]>`, i.e. the `/tasks/...` static mount (auth-protected), or `GET /api/v1/download/{task_id}/final-1.mp4`.

**Titles (optional):** `POST /api/v1/social-metadata` `{"video_subject","video_script","language":"auto","platform":"youtube"}` → `data: {title, caption, hashtags[]}`. It's used when a topic has no title/description.

Other routes: `POST /api/v1/subtitle`, `POST /api/v1/audio`, `GET /api/v1/tasks?page=&page_size=`, `DELETE /api/v1/tasks/{id}` (409 while running), `GET|POST /api/v1/musics`, `GET|POST /api/v1/video_materials`, `GET /api/v1/stream/{path}`, `POST /api/v1/scripts`, `POST /api/v1/terms`.

### YouTube upload behaviour

- Scope is `youtube.upload` only, using the desktop OAuth flow with `access_type=offline`. The token is refreshed automatically and saved to `.credentials/youtube.token.json`.
- Uploads are resumable (8 MiB chunks). HTTP 500/502/503/504 and connection errors are retried up to 10 times with exponential backoff and jitter. `403 quotaExceeded`/`uploadLimitExceeded` stops the run for the day.
- Every upload sets `status.containsSyntheticMedia: true` and `privacyStatus: "private"`. `unlisted` is allowed per topic, and `public` is refused in both the model and the request builder.
- **Quota:** a local ledger (`output/quota_ledger.json`, keyed by Pacific date since quota resets at midnight PT) charges `YOUTUBE_UPLOAD_UNIT_COST` per *attempt* against 10,000 units, with a hard cap of 5 attempts/day. Remaining budget is logged before and after each upload.
  - Note: Google's revision history (2025-12-04) says an upload now costs about 100 units instead of about 1,600. The default stays at the conservative 1,600, and the 5/day cap applies regardless.
- The uploader only reads `output/approved/` and skips any video whose sha256 changed since generation.

### Background music

`make vendor` deletes MPT's bundled `resource/songs/*.mp3`. Upstream's README says they come from YouTube videos and should be deleted if copyright is a concern. Topics default to `bgm_type: ""`. To add music, upload tracks you have a license for via `POST /api/v1/musics` and set `bgm_file` per topic.