# Bundled songs removed

The `output*.mp3` tracks the video engine ships in this folder were deleted by
`make vendor` in youtube-automations. Upstream's own README says they are
default music taken from YouTube videos and asks users to delete them if there
are copyright issues — not something to publish on a monetised channel.

The pipeline defaults to `bgm_type: ""` (no music). To add music, upload
tracks you have a license for via `POST /api/v1/musics` (stored in
the engine's `storage/bgm`) and set `bgm_file` per topic in `topics/queue.yaml`.
