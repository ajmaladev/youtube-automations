#!/usr/bin/env bash
# Vendor the video engine: the pinned upstream commit (deploy/engine.ref) plus this project's engine changes
# (deploy/engine.patch), with the bundled songs removed. Used by `make vendor` and before every CI image build,
# so local and CI engines are byte-for-byte the same code.
# usage: bash scripts/ci/vendor-engine.sh [commit-sha]
set -euo pipefail

ref="${1:-$(tr -d '[:space:]' < deploy/engine.ref)}"
repo="${ENGINE_REPO:-https://github.com/harry0703/MoneyPrinterTurbo.git}"
dir="vendor/video-engine"

retry() {
  for attempt in 1 2 3; do
    "$@" && return 0
    echo "attempt $attempt failed: $*" >&2
    sleep $((attempt * 20))
  done
  return 1
}

rm -rf "$dir"
mkdir -p "$dir"
git -C "$dir" init --quiet
retry git -C "$dir" fetch --quiet --depth 1 "$repo" "$ref"
git -C "$dir" checkout --quiet FETCH_HEAD
if [ -s deploy/engine.patch ]; then
  git -C "$dir" apply --whitespace=nowarn "$PWD/deploy/engine.patch"
fi
rm -rf "$dir/.git"
# bundled songs are removed for copyright reasons (see deploy/songs-README.md)
find "$dir/resource/songs" -type f -delete
cp deploy/songs-README.md "$dir/resource/songs/README.md"
echo "vendored video engine $ref into $dir"
