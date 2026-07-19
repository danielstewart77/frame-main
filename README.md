# frame-main

A minimal, single-box agent system: one FastAPI control plane wraps agent
harnesses (`claude`, `codex`, ...) as sessions, one isolated workspace per user, with
a web console and a Telegram surface. Each session runs in a pristine container;
work persists to per-user git repos.

See [DESIGN.md](DESIGN.md) for the full architecture.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # defaults run fully offline
.venv/bin/python agent-server.py
```

Out of the box `FRAME_PROVISIONER=fake` and `FRAME_VOICE=fake`, so the whole
control plane — sessions, turns, streaming, git durability, surface bindings —
runs with no Docker daemon, no provider account, and no Azure reachability.
Switching both to their real backends is configuration only; see
[docs/vpn-cutover.md](docs/vpn-cutover.md).

```bash
.venv/bin/python -m pytest
```

The Telegram surface is a separate process that speaks the same HTTP API:

```bash
.venv/bin/python surfaces/telegram-bot.py
```

## What is built

The control plane: the registry schema, per-user workspaces with bare-repo
durability, session lifecycle (provision, resume, idle-stop, archive, delete),
harness argv and stream normalisation for `claude` and `codex`, the HTTP and
WebSocket API, voice, and the Telegram engage/disengage model.

The web console is designed but not yet built; the API it needs is built and
tested. The live-app reverse proxy and the interactive TUI pane are likewise
still ahead.
