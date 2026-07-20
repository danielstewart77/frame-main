# frame-main

A minimal, single-box agent system: one FastAPI control plane wraps agent
harnesses (`claude`, `codex`, ...) as sessions, one isolated workspace per user, with
a web console and an optional per-user Telegram bot. Each session runs in a
pristine container; work persists to per-user git repos.

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

Telegram is optional and per-user. There is no shared bot and no shared token:
each user creates a bot with BotFather and pastes its token on the console
settings screen (`PUT /users/{id}/telegram`). The control plane supervises one
long-poll loop per configured bot in-process, so there is nothing separate to
run. A personal bot answers only its owner — the first chat to message it is
enrolled and locked in.

## Authentication

Every route but the health probe, the login endpoints, and the console shell
requires a bearer token. Three kinds of caller hold one:

- a **user** logs in at the console (`POST /auth/login`) and gets a token,
  returned in the body and as an `httponly` cookie. A user sees only their own
  sessions; another account's session id answers 404, not 403.
- a **service** — the operator/admin credential — holds `FRAME_SERVICE_TOKEN`
  and drives the fleet routes (mint and list users, resolve a chat identity to
  an account) plus registration once the box is claimed.
- a **session shim** inside a container holds a token minted for one session at
  spawn, and can drain that session's channel and no other.

The first account on a fresh box is claimed with an open `POST /auth/register`;
once one credential exists, registration takes the service token. Passwords are
stored as scrypt digests and tokens as their sha256, so a readable database
yields no usable credential.

## What is built

The control plane: the registry schema, per-user workspaces with bare-repo
durability, session lifecycle (provision, resume, idle-stop, archive, delete),
harness argv and stream normalisation for `claude` and `codex`, the HTTP and
WebSocket API, token authentication and per-user session isolation, session
event persistence, voice, and the Telegram engage/disengage model.

The web console is built — a login gate, a session rail, live turn streaming
over the WebSocket, the live-app reverse-proxy pane, and the interactive TUI
pane.
