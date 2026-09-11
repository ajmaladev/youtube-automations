#!/usr/bin/env bash
# Start the video engine container on 127.0.0.1:${ENGINE_PORT:-8080} and wait until it answers /ping.
# The engine config is rendered from config/config.template.toml at start, exactly like docker-compose.yml.
# usage: bash scripts/ci/start-engine.sh <image>
set -euo pipefail

image="$1"
name="${ENGINE_CONTAINER:-yta-engine}"
port="${ENGINE_PORT:-8080}"
root="${ENGINE_ROOT:-$PWD}"

docker rm -f "$name" >/dev/null 2>&1 || true
docker run --detach --name "$name" --publish "127.0.0.1:${port}:8080" \
  --env PEXELS_API_KEY --env ENGINE_API_KEY --env LLM_PROVIDER --env OLLAMA_HOST \
  --volume "$root/config/config.template.toml:/opt/yta/config.template.toml:ro" \
  --volume "$root/orchestrator/render_config.py:/opt/yta/render_config.py:ro" \
  "$image" \
  sh -c 'python3 /opt/yta/render_config.py /opt/yta/config.template.toml /app/config.toml && exec python3 main.py' \
  >/dev/null

for i in $(seq 1 120); do
  if curl --silent --fail "http://127.0.0.1:${port}/ping" >/dev/null 2>&1; then
    echo "video engine ready after ~$((i * 2))s"
    exit 0
  fi
  if [ "$(docker inspect --format '{{.State.Running}}' "$name")" != "true" ]; then
    break
  fi
  sleep 2
done

echo "::error::the video engine did not start"
docker logs --tail 200 "$name"
exit 1
