# frame-main — minimal per-user mind

A single-box agent system: one FastAPI mind server is the **control plane**. It
provisions a pristine container per session and runs the harness (`claude`,
`codex`, ...) inside it. One isolated mind per user, surfaces (Telegram + web)
over HTTP. No custom agent loop. The harness *is* the agent.

## Principles

- **Wrap, don't rebuild.** `mind_server` only spawns `claude --resume`, streams
  json, and pipes IO. Turn loop, tools, edits, build-test-fix all live inside
  the harness.
- **Durable side effects, not durable process.** Resume replays the transcript,
  not in-flight work. Every turn commits and *pushes* to a per-user bare repo on
  the host, so work survives even when the container is thrown away. Idempotent
  operations make a replayed turn harmless.
- **Control plane owns Docker; the agent never does.** Only `mind_server` touches
  the Docker socket. It provisions one pristine container per session; the harness
  runs inside with no socket access. No docker-in-docker, no privilege leak, no
  agent spawning containers.
- **Isolated mind per user.** Per-user memory, identity, and session lineage. The
  user id is a boundary, not just a filter.
- **The session is the unit, not the agent.** A session row carries its own
  harness, model, and pristine container. There is no long-lived "agent" object
  that owns sessions — the session *is* the agent instance for its lifetime.
  Spawn = a new row + container; ditch = status `archived` + container removed;
  return = resume. Both the ChatGPT-style sidebar and the fan-out-twelve desktop
  flow are just views over the same table.
- **Multi-user schema now, multi-user machinery later.** Every table carries
  `user_id` from day one. Login flows, quotas, and process isolation wait until a
  real second user exists.

## Layout

```
frame-main/
├── mind_server.py          # control plane: provision containers, stream, track session
├── sandbox/
│   ├── Dockerfile          # base dev image (toolchain + the harness CLIs)
│   ├── provision.py        # docker run/exec/rm + reverse-proxy registration
│   └── entrypoint.sh       # clone session branch, run harness inside container
├── surfaces/
│   ├── telegram_bot.py      # chat_id → user_id
│   └── web/                 # web app → user_id, diff viewer, live-app proxy (auth later)
├── voice/                   # Azure Whisper STT + Azure TTS, inline
├── hooks/
│   └── stop_commit.sh       # commit + push session branch every turn (runs in container)
├── db/
│   ├── registry.db          # central: users + sessions (SQLite)
│   └── schema.sql
├── users/                   # per-user host state (gitignored) — outlives containers
│   └── <user_id>/
│       ├── origin.git/     # per-user BARE repo; sessions push branches here
│       ├── memory.db        # per-user memory, shared across the user's sessions
│       └── identity.md      # this user's soul seed (asked, never inferred)
└── tests/
```

Containers are ephemeral and pristine. Everything that must persist — git
history, memory, identity — lives on the host under `users/<user_id>/` and is
mounted (memory, identity) or pushed to (`origin.git`) from the container.

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
  harness      TEXT NOT NULL,           -- 'claude' | 'codex' | ...
  model        TEXT NOT NULL,           -- e.g. 'opus'
  branch       TEXT NOT NULL,           -- git branch in origin.git for this session
  container_id TEXT,                    -- docker container (null when not running)
  app_port     INTEGER,                 -- reverse-proxied port for the live app (nullable)
  resume_id    TEXT,                    -- harness --resume id (null until first turn)
  transcript   TEXT,                    -- path to harness rollout jsonl (host-mounted)
  status       TEXT NOT NULL DEFAULT 'active',  -- active | done | archived
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
```

The `resume_id` lives in SQLite, never only in process memory — a restart must be
able to find and resume every session. A `/switch` on any surface just repoints
its `surface_bindings` row, which is the whole fix for "Telegram is stuck on one
session."

## Two views, one table

- **Desktop fan-out.** Click plus, pick a harness and model, get a fresh session.
  Kick off a dozen in parallel; each runs in its own pristine container so they
  never touch each other. When done, archive or delete (container removed).
- **Mobile / Telegram.** List your sessions, resume one, or `/new` to create one.
  The surface binding tracks which session is current; `/switch <id>` repoints it.

Both are UI over the `sessions` table. No separate code path.

## Session lifecycle (control plane)

`mind_server` resolves the surface identity to a `user_id`, resolves (or creates)
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

Concurrency is bounded by a semaphore sized to the box and the provider account.

## Pull-down and browse

- **Pull to local.** Session work is a branch in `users/<user_id>/origin.git`, so
  `git fetch`/`git clone` from that per-user repo pulls it straight to your laptop.
- **View diffs.** The web surface reads `git diff` for the session branch and
  renders it — no container access needed once the branch is pushed.
- **Drive the live app.** The container exposes the app on `app_port`; the control
  plane reverse-proxies it to a per-session URL, so you click and interact with the
  running app in the browser with zero local setup.

## Frequent-commit safety

Two layers, both cheap:

- **`hooks/stop_commit.sh`** — a Stop hook running *inside the container* that does
  `git add -A && git commit && git push origin <branch>` after every turn. Because
  it pushes to the host bare repo, nothing is lost even if the container dies;
  worst case a resumed turn re-does its tail against pushed state.
- **System-prompt discipline** — the mind is told to commit logically as it works
  (durable side effects), so history is meaningful, not one giant blob per turn.

## Voice

Azure Whisper for speech-to-text, an Azure neural voice for text-to-speech,
called inline from the surface or the mind server. No separate container.

## Deferred (multi-user machinery)

Real auth on the web surface, per-user rate limits/quotas, per-user process
pools, and an onboarding flow for unknown surface identities. The schema already
supports all of it; build when a second user is real.
