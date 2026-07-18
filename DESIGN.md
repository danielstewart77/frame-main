# mindkit — minimal per-user mind

A single-box agent system: one FastAPI mind server wraps the `claude` CLI as a
subprocess, one isolated mind per user, surfaces (Telegram + web) over HTTP.
No custom agent loop. The harness *is* the agent.

## Principles

- **Wrap, don't rebuild.** `mind_server` only spawns `claude --resume`, streams
  json, and pipes IO. Turn loop, tools, edits, build-test-fix all live inside
  the harness.
- **Durable side effects, not durable process.** Resume replays the transcript,
  not in-flight work. Safety comes from committing to git every turn and keeping
  operations idempotent, so a replayed turn is harmless.
- **Isolated mind per user.** Per-user memory, identity, and session lineage. The
  user id is a boundary, not just a filter.
- **The session is the unit, not the agent.** A session row carries its own
  harness, model, and git worktree. There is no long-lived "agent" object that
  owns sessions — the session *is* the agent instance for its lifetime. Spawn = a
  new row + subprocess; ditch = status `archived`; return = resume. Both the
  ChatGPT-style sidebar and the fan-out-twelve desktop flow are just views over
  the same table.
- **Multi-user schema now, multi-user machinery later.** Every table carries
  `user_id` from day one. Login flows, quotas, and process isolation wait until a
  real second user exists.

## Layout

```
mindkit/
├── mind_server.py          # FastAPI: spawn/resume harness, stream, track session
├── surfaces/
│   ├── telegram_bot.py      # chat_id → user_id
│   └── web/                 # web app → user_id (auth later)
├── voice/                   # Azure Whisper STT + Azure TTS, inline (no container)
├── hooks/
│   └── stop_commit.sh       # auto-commit the user's workspace after every turn
├── db/
│   ├── registry.db          # central: users + sessions (SQLite)
│   └── schema.sql
├── users/                   # per-user isolation root (gitignored)
│   └── <user_id>/
│       ├── repo/            # the user's project repo (main working tree)
│       ├── worktrees/       # one git worktree per active session
│       │   └── <session_id>/
│       ├── memory.db        # per-user memory, shared across the user's sessions
│       └── identity.md      # this user's soul seed (asked, never inferred)
└── tests/
```

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

-- one row per task/topic session; the session owns its harness + model + worktree
CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,        -- our stable session uuid
  user_id      TEXT NOT NULL REFERENCES users(user_id),
  title        TEXT,                    -- topic/task label, editable
  harness      TEXT NOT NULL,           -- 'claude' | 'codex' | ...
  model        TEXT NOT NULL,           -- e.g. 'opus'
  worktree     TEXT NOT NULL,           -- git worktree path for this session
  resume_id    TEXT,                    -- harness --resume id (null until first turn)
  transcript   TEXT,                    -- path to harness rollout jsonl
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
  Kick off a dozen in parallel; each runs in its own worktree so they never
  clobber each other's files. When done, archive or delete.
- **Mobile / Telegram.** List your sessions, resume one, or `/new` to create one.
  The surface binding tracks which session is current; `/switch <id>` repoints it.

Both are UI over the `sessions` table. No separate code path.

## Harness spawn contract

`mind_server` resolves the surface identity to a `user_id`, resolves (or creates)
the target session, ensures its git worktree exists, then spawns the session's
declared harness — for `claude`:

```
claude -p --output-format stream-json --include-partial-messages \
       [--resume <resume_id>] \
       --append-system-prompt "<identity + memory blocks>"
```

with `cwd = users/<user_id>/worktrees/<session_id>` and env scoped to that user
(their `user_id`, their `memory.db` path). It reads the harness session id off the
first event, writes it to `resume_id`, and upserts the row. Concurrency is bounded
by a semaphore sized to the box and the provider account.

## Frequent-commit safety

Two layers, both cheap:

- **`hooks/stop_commit.sh`** — a harness Stop hook that runs `git add -A &&
  git commit` in the session's worktree after every turn. Nothing is ever lost to
  a crash; worst case a resumed turn re-does its tail against committed state.
- **System-prompt discipline** — the mind is told to commit logically as it works
  (durable side effects), so history is meaningful, not one giant blob per turn.

## Voice

Azure Whisper for speech-to-text, an Azure neural voice for text-to-speech,
called inline from the surface or the mind server. No separate container.

## Deferred (multi-user machinery)

Real auth on the web surface, per-user rate limits/quotas, per-user process
pools, and an onboarding flow for unknown surface identities. The schema already
supports all of it; build when a second user is real.
