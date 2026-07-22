#!/usr/bin/env bash
# Container entrypoint: clone the session branch off the mounted bare repo,
# install the stop hook, then idle so the control plane can `docker exec` turns.
set -euo pipefail

ORIGIN="${GIT_ORIGIN:-/origin.git}"
BRANCH="${FRAME_BRANCH:?FRAME_BRANCH is required}"
REPO=/workspace/repo
# Runs as the host user (see provision.py --user); HOME is writable /workspace.
export HOME="${HOME:-/workspace}"

git config --global user.name "frame-main agent"
git config --global user.email "agent@frame-main.local"
git config --global --add safe.directory "$ORIGIN"
git config --global --add safe.directory "$REPO"

if [ ! -d "$REPO/.git" ]; then
  if git --git-dir="$ORIGIN" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git clone --branch "$BRANCH" "$ORIGIN" "$REPO"
  else
    git clone "$ORIGIN" "$REPO" 2>/dev/null || git init "$REPO"
    cd "$REPO"
    git remote add origin "$ORIGIN" 2>/dev/null || true
    git checkout -b "$BRANCH"
  fi
fi

# The Stop hook pushes every turn, so nothing is lost when this container dies.
# Installing the script is not enough: Claude Code only runs a hook that is
# declared in settings.json, so the declaration is the part that makes it fire.
mkdir -p "$HOME/.claude/hooks"
install -m 755 /opt/frame/hooks/stop-commit.sh "$HOME/.claude/hooks/stop-commit.sh"
cat > "$HOME/.claude/settings.json" <<JSON
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/stop-commit.sh"
          }
        ]
      }
    ]
  }
}
JSON

cd "$REPO"
exec sleep infinity
