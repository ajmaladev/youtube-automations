#!/usr/bin/env bash
# Build the video engine image from the pinned, patched engine source (see vendor-engine.sh).
# usage: bash scripts/ci/build-engine.sh <commit-sha> <image-tag>
set -euo pipefail

ref="$1"
image="$2"

bash scripts/ci/vendor-engine.sh "$ref"
for attempt in 1 2 3; do
  docker build --file deploy/engine.Dockerfile --tag "$image" vendor/video-engine && exit 0
  echo "docker build attempt $attempt failed" >&2
  sleep $((attempt * 30))
done
exit 1
