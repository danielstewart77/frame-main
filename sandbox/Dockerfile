# Base sandbox image: toolchain + harness CLIs. One pristine container per session.
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq ripgrep python3 python3-pip python3-venv build-essential \
    && rm -rf /var/lib/apt/lists/*

# Harness CLIs. The agent runs these; it never sees a Docker socket.
RUN npm install -g @anthropic-ai/claude-code @openai/codex

WORKDIR /workspace
RUN mkdir -p /workspace/repo /workspace/transcripts /opt/frame/hooks

# The channel shim the harness spawns over stdio. Its own venv: bookworm refuses
# system pip installs, and this is the only Python in the container.
COPY sandbox/requirements.txt /opt/frame/requirements.txt
RUN python3 -m venv /opt/frame/venv \
    && /opt/frame/venv/bin/pip install --no-cache-dir -r /opt/frame/requirements.txt
COPY sandbox/channel.py /opt/frame/channel.py
# Lives outside /workspace/repo so the session's own git history stays clean.
COPY sandbox/mcp.json /opt/frame/mcp.json

COPY hooks/stop-commit.sh /opt/frame/hooks/stop-commit.sh
COPY sandbox/entrypoint.sh /opt/frame/entrypoint.sh
RUN chmod 755 /opt/frame/hooks/stop-commit.sh /opt/frame/entrypoint.sh

# The live app the session builds is served here and reverse-proxied by app_port.
EXPOSE 3000

ENTRYPOINT ["/opt/frame/entrypoint.sh"]
