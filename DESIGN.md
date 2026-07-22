# frame-main — minimal per-user agent-server

A single-box agent system: one FastAPI **agent-server** is the **control plane**.
It provisions a pristine container per session and runs the harness (`claude`,
`codex`, ...) inside it. One isolated workspace per user, surfaces (Telegram + web)
over HTTP. No custom agent loop. The harness *is* the agent.

## Principles

- **Wrap, don't rebuild.** `agent-server` only spawns `claude --resume`, streams
  json, and pipes IO. Turn loop, tools, edits, build-test-fix all live inside
  the harness.
- **Durable side effects, not durable process.** Resume replays the transcript,
  not in-flight work. Every turn commits and *pushes* to a per-user bare repo on
  the host, so work survives even when the container is thrown away. Idempotent
  operations make a replayed turn harmless.
- **Control plane owns Docker; the agent never does.** Only `agent-server` touches
  the Docker socket. It provisions one pristine container per session; the harness
  runs inside with no socket access. No docker-in-docker, no privilege leak, no
  agent spawning containers.
- **Isolated workspace per user.** Per-user memory, identity, and session lineage.
  The user id is a boundary, not just a filter.
- **The session is the unit, not the agent.** A session row carries its own
  harness, model, and pristine container. There is no long-lived "agent" object
  that owns sessions — the session *is* the agent instance for its lifetime.
  Spawn = a new row + container; ditch = status `archived` + container removed;
  return = resume. Both the ChatGPT-style sidebar and the fan-out-twelve desktop
  flow are just views over the same table.
- **Multi-user schema now, multi-user machinery later.** Every table carries
  `user_id` from day one. Login and per-user session isolation are built; quotas
  and per-user process pools wait until they are worth the machinery.

## Layout

```
frame-main/
├── agent-server.py          # control plane entrypoint (run, never imported)
├── server.py                # the HTTP API — importable app factory
├── sessions.py              # session lifecycle: provision, turn, idle, teardown
├── registry.py              # SQLite registry: users, identities, sessions, bindings
├── workspace.py             # per-user host state: origin.git, memory.db, identity.md
├── harness.py               # argv construction + stream-json normalisation
├── voice.py                 # Azure STT/TTS behind an interface, with an offline fake
├── config.py                # env-resolved settings
├── sandbox/
│   ├── Dockerfile           # base dev image (toolchain + the harness CLIs)
│   ├── provision.py         # docker run/exec/rm; DockerProvisioner + FakeProvisioner
│   └── entrypoint.sh        # clone session branch, install hook, await exec'd turns
├── surfaces/
│   ├── chat.py              # surface-agnostic engage/disengage routing
│   └── telegram.py          # in-process supervisor: one long-poll bot per user
├── proxy.py                 # per-session reverse proxy for the browser pane
├── console/                 # web console: index.html + console.css + console.js
├── hooks/
│   └── stop-commit.sh       # commit + push session branch every turn (runs in container)
├── db/
│   ├── registry.db          # central: users + sessions (SQLite)
│   └── schema.sql
├── users/                   # per-user host state (gitignored) — outlives containers
│   └── <user_id>/
│       ├── origin.git/      # per-user BARE repo; sessions push branches here
│       ├── memory.db        # per-user memory, shared across the user's sessions
│       └── identity.md      # this user's soul seed (asked, never inferred)
└── tests/
```

Containers are ephemeral and pristine. Everything that must persist — git
history, memory, identity — lives on the host under `users/<user_id>/` and is
mounted (memory, identity) or pushed to (`origin.git`) from the container.

**Naming.** Component, file, and service names use hyphens, never underscores
(`agent-server`, `stop-commit.sh`) — underscores are painful over
voice. Hyphenated `.py` files (`agent-server.py`) are **entrypoints run
directly** (`python agent-server.py`), never imported; importable logic lives in
single-word modules so no module name ever needs an underscore. SQL columns and
in-code identifiers (`user_id`, `session_id`) keep underscores — hyphens are
illegal there and they aren't spoken names.

## Registry schema (central)

```sql
CREATE TABLE users (
  user_id      TEXT PRIMARY KEY,        -- uuid
  display_name TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active'
);

-- surface identity → user (a Telegram chat, a web login, etc.)
CREATE TABLE identities (
  surface      TEXT NOT NULL,           -- 'telegram' | 'web'
  external_id  TEXT NOT NULL,           -- telegram chat_id, web user id
  user_id      TEXT NOT NULL REFERENCES users(user_id),
  PRIMARY KEY (surface, external_id)
);

-- one row per task/topic session; the session owns its harness + model + container
CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,        -- our stable session uuid
  user_id      TEXT NOT NULL REFERENCES users(user_id),
  title        TEXT,                    -- topic/task label, editable
  color        TEXT,                    -- optional accent color for the frame + session list, editable
  harness      TEXT NOT NULL,           -- 'claude' | 'codex' | ...
  model        TEXT NOT NULL,           -- e.g. 'opus'
  branch       TEXT NOT NULL,           -- git branch in origin.git for this session
  container_id TEXT,                    -- docker container (null when not running)
  app_port     INTEGER,                 -- reverse-proxied port for the live app (nullable)
  resume_id    TEXT,                    -- harness --resume id (null until first turn)
  transcript   TEXT,                    -- path to harness rollout jsonl (host-mounted)
  status       TEXT NOT NULL DEFAULT 'active',  -- active | done | archived
  frame_state  TEXT NOT NULL DEFAULT 'closed',  -- closed | docked | minimized
  speaker      INTEGER NOT NULL DEFAULT 0,      -- per-frame spoken playback toggle
  created_at   TEXT NOT NULL,
  last_active  TEXT NOT NULL
);

-- a surface's "current" session is just a repointable pointer
CREATE TABLE surface_bindings (
  surface      TEXT NOT NULL,           -- 'telegram' | 'web'
  external_id  TEXT NOT NULL,           -- telegram chat_id
  session_id   TEXT NOT NULL REFERENCES sessions(id),
  PRIMARY KEY (surface, external_id)
);

-- per-surface console state that belongs to no single session
CREATE TABLE surface_layout (
  surface           TEXT NOT NULL,
  external_id       TEXT NOT NULL,
  sidebar_collapsed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (surface, external_id)
);
```

Frame layout is a property of the surface, not the browser tab, so it lives
here: each session carries its own `frame_state` and `speaker` preference, and
the sidebar's collapsed state hangs off `surface_layout`. Reopening the console
replays both.

The `resume_id` lives in SQLite, never only in process memory — a restart must be
able to find and resume every session. A `/switch` on any surface just repoints
its `surface_bindings` row, which is the whole fix for "Telegram is stuck on one
session."

Because a restart also strands containers, `SessionManager.recover()` runs once
at startup and reconciles the table against what docker is still running (matched
by a `frame.session` label). A container that survived the restart is re-adopted
untouched — the next turn re-execs its harness with `--resume`. A container that
did not is cleared from its row so the next turn re-provisions cleanly from
`origin.git`, and a live container no session still claims is removed as an
orphan. Recovery never deletes a session: a stranded container is reconciled, not
a reason to lose work. This makes the "start a project Friday, resume it two
weeks later" case fall out for free — the idle reaper only stops containers,
sessions never expire, and recovery restores whatever the machine forgot.

## Two views, one table

- **Desktop fan-out.** Click plus, pick a harness and model, get a fresh session.
  Kick off a dozen in parallel; each runs in its own pristine container so they
  never touch each other. When done, archive or delete (container removed).
- **Mobile / Telegram.** List your sessions, resume one, or `/new` to create one.
  The surface binding tracks which session is current; `/switch <id>` repoints it.

Both are UI over the `sessions` table. No separate code path.

## Session lifecycle (control plane)

`agent-server` resolves the surface identity to a `user_id`, resolves (or creates)
the target session, then:

1. **Provision.** `docker run` a pristine container from the base image, mounting
   the user's `memory.db` and `identity.md` read paths and injecting env
   (`user_id`, session `branch`, provider creds). Record `container_id`.
2. **Run harness inside.** `entrypoint.sh` clones the session `branch` from
   `users/<user_id>/origin.git`, then execs the harness — for `claude`:

   ```
   claude -p --output-format stream-json --include-partial-messages \
          [--resume <resume_id>] \
          --append-system-prompt "<identity + memory blocks>"
   ```

   The control plane streams stdout back to the surface, reads the harness session
   id off the first event, writes it to `resume_id`.
3. **Idle / teardown.** Idle containers are stopped (state persists in
   `origin.git`); a resume re-provisions and re-clones. Archiving a session removes
   the container for good; the branch stays.

Concurrency is bounded by a semaphore sized to the box and the provider account,
and a single turn is bounded by `FRAME_TURN_TIMEOUT_SECONDS`. The harness retries
an unreachable provider ten times with backoff before giving up, so an unbounded
turn would hold its slot and stream nothing; the timeout closes the stream and
emits an `error` event instead.

### Harness process model: persistent, with a channel for inbound events

The harness runs as **one long-lived process per session**, held open for the
session's life rather than respawned per turn:

```
claude -p --input-format stream-json --output-format stream-json \
       --include-partial-messages \
       --dangerously-skip-permissions \
       --mcp-config /opt/frame/mcp.json \
       --dangerously-load-development-channels server:frame \
       [--resume <resume_id>] \
       --append-system-prompt "<identity + memory blocks>"
```

`--input-format stream-json` accepts realtime streaming input on stdin, so a
persistent process and the structured event contract this doc's
surface-normalisation table is built on are not in tension: the control plane
writes a user message to stdin and reads the same `session`, `text`, `tool`,
`status`, `result`, `error` events off stdout it already normalises. Per-turn
respawn buys nothing over this and costs a cold start plus a `--resume`
round-trip on every turn.

Persistence alone does not give unsolicited output. An idle stream-json process
emits nothing until something arrives on stdin, so a background Task-tool
subagent finishing still surfaces only when the next turn opens. **Channels**
close that gap. A channel is an MCP server that Claude Code spawns over stdio and
that pushes `notifications/claude/channel` events into the *already-running*
session; each arrives in context as a `<channel source="…">` tag and opens a turn.

Because the harness spawns it over stdio, a channel cannot be a remotely-hosted
server — but it doesn't need to hold any logic. `sandbox/channel.py` is a relay:
it long-polls `/sessions/{id}/channel/events` on the control plane and emits each
event as a channel notification, and its `reply` tool POSTs to
`/sessions/{id}/channel/reply`. Sender allowlisting, surface fan-out and routing
stay in the control plane, the only component that knows about users. The shim is
registered from `/opt/frame/mcp.json`, outside `/workspace/repo` so the session's
own git history stays clean. This gives the control plane three things a per-turn
batch process cannot have:

- **Inbound wake.** Anything — a finished background job, a CI webhook, a
  scheduler, a Telegram message — queues an event on the control plane, and the
  session reacts without waiting for the user.
- **Outbound reply.** The channel exposes a `reply` tool, so the agent routes
  messages back to the originating surface mid-turn rather than only in its
  final result.

`sandbox/harness_process.py` holds that process: one `docker exec -i` per
session, prompts written to stdin as stream-json, one reader task over stdout.
Turns are serialised — the harness runs one at a time, so a second prompt written
mid-turn would have its output interleaved with the first and be unattributable.
Anything emitted while no turn is outstanding is unsolicited by definition, and
goes to the session's bus rather than to a requester that doesn't exist; that is
the wake path arriving. Interrupt is an in-band `control_request` rather than a
signal, because signalling the process would take down the session context the
persistent form exists to keep. The harness outlives its turns but not its
container: stopping or removing a container closes it. `codex exec` is one-shot
and has no stdin form, so that harness stays on the per-turn path.

Gate inbound events on **sender identity** before emitting a notification — an
ungated channel is a prompt-injection path straight into the session's context,
and a session running with approvals off will act on whatever lands there. This
is why `POST /sessions/{id}/channel/deliver` takes the session owner's authority
(a service surface, or the user who owns it) rather than being an open door: it
writes into the running session, and the container itself never fills that queue,
it only drains it.

### The transcript outlives the process

A session's events go to its bus, which fans them out to whoever is attached and
keeps a bounded in-memory tail for reconnects. For a session running unattended
that is nobody, and the tail dies with the process — so every event is also
written to `session_events` as it is published.

Text is coalesced: the harness emits a token at a time and a row per token would
be a row per token, so a contiguous run of `text` becomes one row stamped with
the `seq` of its first event. Transcript order and bus order therefore agree, and
`GET /sessions/{id}/events` pages by `seq` the same way a reconnecting surface
does. The write is idempotent on `(session_id, seq)`, and a sink that throws is
logged rather than raised — persistence failing must not cost a live surface its
event.

`sessions.outcome` is nulled when a turn starts and set to `ok` or `error` when
one lands, so a list of twelve sessions can show which finished, which failed,
and which are still going without opening any of them. The transcript survives
archiving, because reading back a finished session is the entire point of it, and
is deleted only with the session itself.

### Sessions run unattended

Both harnesses spawn with approvals off: `--dangerously-skip-permissions` for
claude, `--dangerously-bypass-approvals-and-sandbox` for codex. A session starts,
runs, and finishes without asking anyone anything.

This is not a shortcut around a safety gate; it is the only coherent reading of
the deployment. Nobody is at a terminal inside the container, so an approval
prompt has no one to answer it — it stalls the turn until something times out,
and a timeout has to guess a verdict from silence. Guessing *deny* stops the
session; guessing *allow* is the flag with extra steps and a delay. Neither is
better than not asking. The failure mode that matters here is a fleet of a dozen
sessions where eleven come back stopped because no human clicked anything, and
the operator's attention is the scarce resource the whole design is spending.

The sandbox boundary is therefore the **container**, not an in-harness prompt: a
session gets a workspace, a network position, and credentials, and whatever it
can reach with those it may use without asking. Narrowing what a session may do
means narrowing that grant — a tighter mount, a scoped token, a different network
— not interposing a question. Correspondingly, an approval prompt is not
available as a containment mechanism, so anything a session must not do has to be
made unreachable rather than merely gated.

Constraints to hold in mind:

- Channels are a research preview. Only Anthropic-allowlisted plugins register;
  frame-main's own channel needs `--dangerously-load-development-channels
  server:frame` until it is listed, and the flag syntax and protocol contract may
  change.
- Channels require Anthropic auth via claude.ai or a Console API key and are
  unavailable on Bedrock, Google Cloud's Agent Platform, and Microsoft Foundry.
  Verify against the inference-proxy `ANTHROPIC_BASE_URL` wiring before relying
  on them.
- Events queue and are delivered as a group on the next turn if several arrive
  while the session is busy. Independent event streams need separate sessions.

**Remote Control is not the mechanism here**, despite the name. It is a
persistent local session that does push subagent and workflow progress and mobile
notifications in real time, but its only clients are claude.ai and the Claude
mobile app via an Anthropic relay; there is no protocol for frame-main's console
to be the client. It also requires claude.ai OAuth, refuses API keys and
`claude setup-token` credentials, and is disabled outright when
`ANTHROPIC_BASE_URL` points anywhere other than `api.anthropic.com` — which
frame-main's inference proxy does.

## HTTP API

Every surface — web console, Telegram bot, curl — speaks only this contract.

```
GET    /health                                  (public)
POST   /auth/register                           {username, password, display_name?} (public until first account)
POST   /auth/login                              {username, password} -> {token, user_id, expires_at} + cookie
POST   /auth/logout
GET    /auth/me                                 -> {kind, user_id, username}
POST   /users                                   {display_name} (service only)
GET    /users                                   (service only)
POST   /identities                              {surface, external_id, display_name?} -> {user_id} (service only)

POST   /users/{user_id}/sessions                {harness?, model?, title?, color?}
GET    /users/{user_id}/sessions?status=active
GET    /users/{user_id}/frames                  sessions to restore as frames
GET    /sessions/{id}
PATCH  /sessions/{id}                           {title?, color?, status?, frame_state?, speaker?}
DELETE /sessions/{id}

POST   /sessions/{id}/turn                      {prompt} -> ndjson event stream
WS     /sessions/{id}/stream?since=<seq>        subscribe to everything the session emits;
                                                send {prompt} to start a turn; `since` replays
                                                the tail a reconnecting surface missed
POST   /sessions/{id}/channel/deliver           {content, meta?} inbound wake -> {queued}
GET    /sessions/{id}/channel/events?timeout=   shim long poll -> {events}
POST   /sessions/{id}/channel/reply             {chat_id, text} agent reply out
POST   /sessions/{id}/interrupt                 cut an in-flight turn short
POST   /sessions/{id}/start                     provision the container
POST   /sessions/{id}/stop                      stop it; state lives in origin.git
POST   /sessions/{id}/archive                   remove the container, keep the branch
GET    /sessions/{id}/events?after_seq=&limit=   the persisted transcript, read after the fact
GET    /sessions/{id}/diff
GET    /sessions/{id}/clone-url
ANY    /sessions/{id}/app/{path}               browser pane: reverse proxy to app_port
WS     /sessions/{id}/tui                      full terminal: pty on the container

POST   /surfaces/{surface}/{external_id}/attach {session_id}
GET    /surfaces/{surface}/{external_id}/attach
DELETE /surfaces/{surface}/{external_id}/attach
GET    /surfaces/{surface}/{external_id}/layout
PATCH  /surfaces/{surface}/{external_id}/layout {sidebar_collapsed}

POST   /voice/transcribe                        multipart file -> {text}
POST   /voice/speak                             {text, voice?} -> audio/mpeg

GET    /console                                 the console shell (public: the login form)
GET    /console/bootstrap                       identity + layout to restore
```

Every route but `/health`, the `/auth` pair that hands out credentials, and the
`/console` shell requires a bearer token — in the `Authorization` header, or the
`httponly` `frame_auth` cookie the console rides on (a browser cannot attach a
header to a WebSocket handshake, so the cookie carries the socket routes too).

Streamed turns are normalised out of each harness's own json into one small
vocabulary, so a surface renders `claude` and `codex` identically:

| kind | payload | meaning |
|---|---|---|
| `session` | `resume_id` | first event; persisted to `sessions.resume_id` |
| `text` | `text` | assistant output delta |
| `tool` | `name` | a tool call started |
| `status` | `text` | liveness — requesting, provider retry |
| `result` | `text` | turn finished |
| `error` | `text` | turn failed |
| `reply` | `chat_id`, `text` | agent routed a message out through its channel |
| `gap` | `from_seq`, `to_seq` | events in that range aged out before reconnect |
| `raw` | `event` | unrecognised, passed through |

These reach a surface through the session's bus rather than the request that
started the turn, so a frame watching `/sessions/{id}/stream` sees turns opened
by a channel event or by another surface, not only its own.

Every published event carries a monotonic `seq`, and the bus keeps a replay
buffer of the recent tail. A surface that stops draining its socket is
disconnected rather than quietly starved: it reconnects with `?since=<seq>` and
the missed events are replayed first. A slow surface means something went wrong
with that surface, and a visible reconnect that comes back whole is worth more
than a stream that silently develops a hole. If the buffer has already rolled
past the requested point the backfill opens with a `gap` event, so a surface
never renders missing output as if nothing happened.

## Swappable externals

The two pieces that reach the network sit behind interfaces with offline fakes,
so every layer above them is exercised end to end without the proxy or a Docker
daemon. `FRAME_PROVISIONER` selects `fake` or `docker`; `FRAME_VOICE` selects
`fake` or `azure`. Neither switch changes a call site — see
[docs/go-live.md](docs/go-live.md).

## Pull-down and browse

- **Pull to local.** Session work is a branch in `users/<user_id>/origin.git`, so
  `git fetch`/`git clone` from that per-user repo pulls it straight to your laptop.
- **View diffs.** The web surface reads `git diff` for the session branch and
  renders it — no container access needed once the branch is pushed.
- **Drive the live app.** The container exposes the app on `app_port`; the control
  plane reverse-proxies it to a per-session URL, so you click and interact with the
  running app in the browser with zero local setup.

## Web console

The app ships with a web console — the desktop control surface for sessions.

- **Spawn.** A plus button creates a session: pick harness + model, and an **agent
  frame** opens. Fan out many frames at once, tile or focus them.
- **Session sidebar.** A left rail lists the user's sessions (active, with an
  archived view) grouped for scanning; clicking one opens or focuses its frame.
  The rail **collapses** to reclaim width for the grid and its open/collapsed
  state is remembered between visits.
- **The agent frame.** Each frame is one session and **streams by default** — the
  harness output flows live into the frame as an emulated terminal, so you always
  see what the agent is doing without asking. Its main area is the conversation.
  Input is multimodal — type, hold-to-talk voice, image, and drag-and-drop of
  files or folders straight onto the frame, all forwarded to that session's
  harness. A per-frame **speaker toggle** (independent of voice input) turns
  spoken playback of that session's replies on or off, so you can voice a busy
  frame while the rest stay silent; the preference is remembered per session.
- **Sidecar panes.** A frame menu opens panes that expand to the right without
  leaving the conversation:
  - **Browser** — the session's live app, reverse-proxied on its `app_port`, click
    and drive it in place.
  - **Diff** — the session branch's `git diff`, rendered live.
  - **Full terminal (TUI).** Beyond the default read-only stream, a real
    interactive terminal attached to the container (via pty over WebSocket), so you
    can send slash-commands or `$`-commands directly to the harness (e.g. codex
    control commands) and drive raw shell when needed.
- **Frame menu actions.** Per session: rename/title, **recolor** (pick an accent
  color that tints the frame header and the session's list entry so fanned-out
  frames are distinguishable at a glance), archive or delete, `/switch`
  the mobile binding to this session, pull-to-local instructions (the
  `origin.git` branch URL), and **open code** — because the repo is already cloned
  on the host, this launches an editor view (or hands off a local `code <path>`)
  rather than re-cloning. Cloning is only offered when no local copy exists.

The console is pure UI over the existing HTTP API and `sessions` table; it adds no
new session semantics. It lives in `console/` and is served at `/console`; see
[docs/web-console.md](docs/web-console.md) for how it's built and where its
limits are.

### Frame window management

Each frame has a **persistent state** — `docked` (tiled in the grid) or
`minimized` — plus a separate transient **maximized** mode layered on top.

- **Grid.** Docked frames auto-tile; the default tile size is a function of how
  many frames are docked (twelve docked → a twelve-up grid).
- **Minimize.** Sets a frame's persistent state to `minimized` (a strip/tab); the
  remaining docked frames re-tile to fill the space.
- **Restore.** Returns a `minimized` frame to `docked`, rejoining the grid.
- **Maximize.** Promotes one frame to full screen and visually collapses every
  other frame for the duration. This does **not** change the others' persistent
  state — it's an overlay.
- **Un-maximize (restore from maximized).** Every frame returns to its own
  persistent state: docked frames rejoin the grid, and any frame that was already
  `minimized` before the maximize stays minimized. Maximizing then restoring is a
  no-op on the underlying layout.
- **Layout persistence.** The console layout survives a reload or a server
  restart: which sessions are open as frames and each frame's persistent state
  (`docked` / `minimized`), plus the sidebar's collapsed state, are restored on
  return. Because the open set is a property of the surface, not the browser tab,
  it belongs with the session/surface state rather than in client-only storage.

### Mobile web console

The web console is a desktop-first control surface — day-to-day mobile use is the
Telegram surface. When the console *is* opened on a phone, its shell is **locked
to the viewport**: the nav and frame chrome stay fixed and only the conversation
scrolls, so a single frame reads like a chat app instead of the whole page
panning. It collapses to one frame at a time with a back control to the session
list rather than trying to tile a grid on a small screen.

## Telegram surface (engage / disengage)

One bot **per user**, not per agent, and optional: there is no shared bot and no
shared token. Each user creates a bot with BotFather and pastes its token on the
console settings screen (`PUT /users/{id}/telegram`); the control plane
supervises one long-poll loop per configured bot **in-process**, reconciling on a
timer as tokens are added, changed, or removed. A personal bot answers only its
owner — the first chat to message it is enrolled and locked in, and every other
chat is dropped. A Telegram chat is a thin remote that *attaches* to one session
at a time — this is the trickiest part of the system, so it's specified
explicitly.

- **List.** `/agents` (or a persistent menu button) returns an inline keyboard of
  the user's **active** sessions, one button each labelled by title. `/archived`
  lists archived ones the same way. `/new` creates a session (prompting for
  harness + model) and attaches to it.
- **Engage.** Tapping a session button writes that `session_id` into the chat's
  `surface_bindings` row — the chat is now *attached*. Plain messages (text, voice,
  image) from then on route to that session's harness and its streamed replies come
  back debounced via `editMessageText`. A short header confirms which agent you're
  talking to.
- **Disengage.** `/agents` re-lists without losing the attachment; `/detach`
  clears the binding so the chat is idle again; tapping a different session
  repoints the binding. Exactly one session is attached per chat at a time, so
  there's never ambiguity about where a message goes.
- **State lives in the binding, not the bot.** Because attachment is just the
  `surface_bindings` row, a bot restart or a `/switch` from the web console stays
  consistent — the chat re-attaches to whatever the row says.

## Frequent-commit safety

Two layers, both cheap:

- **`hooks/stop-commit.sh`** — a Stop hook running *inside the container* that does
  `git add -A && git commit && git push origin <branch>` after every turn. Because
  it pushes to the host bare repo, nothing is lost even if the container dies;
  worst case a resumed turn re-does its tail against pushed state.
- **System-prompt discipline** — the agent is told to commit logically as it works
  (durable side effects), so history is meaningful, not one giant blob per turn.

## Voice

Azure Whisper for speech-to-text, an Azure neural voice for text-to-speech,
called inline from the surface or the agent-server. No separate container.

## Authentication

Three principals reach the API, and they are not the same. A **user** logs in at
the console and holds a token — returned once in the body and as an `httponly`
cookie — that is scoped to their own account; another user's session id answers
404, not 403, because confirming an id exists is itself a leak across accounts. A
**service** is the operator/admin credential holding `FRAME_SERVICE_TOKEN`; it
drives the fleet routes and can register accounts once the box is claimed, which
is why minting users and resolving identities are service-only. A **session shim** inside a
container holds a bearer minted for one session at spawn and rotated on every
respawn; it may drain that session's channel and no other, so a compromised
container cannot reach its neighbours.

Passwords are stored as scrypt digests and every token as its sha256, so a
readable database yields no usable credential. The plaintext token is returned
exactly once, at login or at spawn. The first account on a fresh box is claimed
with an open `POST /auth/register` — the operator standing in front of it — and
once one credential exists, registration takes the service token.

`auth.py` is all stdlib; the `credentials`, `auth_tokens`, and `session_tokens`
tables carry the persistent side. Login tokens expire on a TTL and are purged on
the same timer that reaps idle containers.

## Deferred (multi-user machinery)

Per-user rate limits/quotas, per-user process pools, and an onboarding flow for
unknown surface identities. The schema and the auth layer already support all of
it; build when a second user is real.
