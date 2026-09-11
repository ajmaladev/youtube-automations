#!/usr/bin/env bash
# Make the pinned video engine image available: pull it from GHCR, or build it once (and push it when
# PUSH_ENGINE_IMAGE=true). Prints the image reference as the last line of stdout.
# usage: bash scripts/ci/engine-image.sh
set -euo pipefail

ref="$(tr -d '[:space:]' < deploy/engine.ref)"
owner="${GITHUB_REPOSITORY_OWNER:-local}"
image="ghcr.io/${owner,,}/yta-video-engine:${ref:0:12}"

if docker pull --quiet "$image" >&2 2>/dev/null; then
  echo "pulled $image" >&2
else
  echo "no cached image yet; building $image from upstream commit $ref" >&2
  bash scripts/ci/build-engine.sh "$ref" "$image" >&2
  if [ "${PUSH_ENGINE_IMAGE:-false}" = "true" ]; then
    docker push --quiet "$image" >&2 || echo "::warning::could not push $image; the next run will build it again" >&2
  fi
fi
echo "$image"
