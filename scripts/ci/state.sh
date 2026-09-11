#!/usr/bin/env bash
# Keep the upload history (output/uploaded/*.json sidecars + quota ledger) on the orphan branch
# "automation-state", so a re-run or a double trigger never posts the same video twice.
# usage: bash scripts/ci/state.sh restore|save    (from the repo root; needs a checkout with push rights to save)
set -euo pipefail

branch="${STATE_BRANCH:-automation-state}"
out="${OUTPUT_DIR:-output}"
dir=".state"

open_state() {
  if [ -d "$dir" ]; then
    return
  fi
  if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    git fetch --quiet --depth 1 origin "$branch"
    git worktree add --quiet -B "$branch" "$dir" FETCH_HEAD
  else
    git worktree add --quiet --orphan -b "$branch" "$dir"
  fi
}

case "${1:-}" in
  restore)
    open_state
    mkdir -p "$out/uploaded"
    if compgen -G "$dir/uploaded/*.json" >/dev/null; then
      cp "$dir"/uploaded/*.json "$out/uploaded/"
    fi
    if [ -f "$dir/quota_ledger.json" ]; then
      cp "$dir/quota_ledger.json" "$out/"
    fi
    echo "restored $(find "$out/uploaded" -name '*.json' | wc -l) upload record(s)"
    ;;
  save)
    open_state
    mkdir -p "$dir/uploaded"
    if compgen -G "$out/uploaded/*.json" >/dev/null; then
      cp "$out"/uploaded/*.json "$dir/uploaded/"
    fi
    if [ -f "$out/quota_ledger.json" ]; then
      cp "$out/quota_ledger.json" "$dir/"
    fi
    git -C "$dir" add --all
    if git -C "$dir" diff --cached --quiet; then
      echo "upload history unchanged"
      exit 0
    fi
    git -C "$dir" -c user.name="github-actions[bot]" \
      -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
      commit --quiet --message "upload history: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for attempt in 1 2 3; do
      git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch" && exit 0
      sleep $((attempt * 10))
    done
    echo "::error::could not save the upload history"
    exit 1
    ;;
  *)
    echo "usage: $0 restore|save" >&2
    exit 2
    ;;
esac
