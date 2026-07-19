#!/usr/bin/env bash
# Stop hook, runs inside the container after every turn.
# Commits whatever the agent left behind and pushes it to the host bare repo,
# so work survives a thrown-away container. Idempotent: a no-op when clean.
set -euo pipefail

REPO="${FRAME_REPO:-/workspace/repo}"
BRANCH="${FRAME_BRANCH:?FRAME_BRANCH is required}"

cd "$REPO" || exit 0

if [ -z "$(git status --porcelain)" ]; then
  git push origin "$BRANCH" >/dev/null 2>&1 || true
  exit 0
fi

git add -A
git commit -m "turn: ${FRAME_SESSION_ID:-session} $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
git push origin "$BRANCH" >/dev/null 2>&1 || git push --set-upstream origin "$BRANCH" >/dev/null
