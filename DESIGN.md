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
- **Isolated mind per user.** Per-user workspace, memory, identity, and session
  lineage. The user id is a boundary, not just a filter.
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
│       ├── workspace/       # the harness cwd — its own git repo
│       ├── memory.db        # this user's memory store
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

-- resumable session lineage, one live row per (user, surface, chat)
CREATE TABLE sessions (
  session_id   TEXT NOT NULL,           -- claude --resume id
  user_id      TEXT NOT NULL REFERENCES users(user_id),
  surface      TEXT NOT NULL,
  chat_id      TEXT NOT NULL,
  transcript   TEXT NOT NULL,           -- path to harness rollout jsonl
  created_at   TEXT NOT NULL,
  last_active  TEXT NOT NULL,
  PRIMARY KEY (user_id, surface, chat_id)
);
```

The session id lives in SQLite, never only in process memory — a restart must be
able to find and resume every conversation.

## Harness spawn contract

`mind_server` resolves the surface identity to a `user_id`, looks up (or creates)
the session row, then spawns:

```
claude -p --output-format stream-json --include-partial-messages \
       [--resume <session_id>] \
       --append-system-prompt "<identity + memory blocks>"
```

with `cwd = users/<user_id>/workspace` and env scoped to that user (their
`user_id`, their `memory.db` path). It reads `session_id` off the first event and
upserts the session row. Concurrency is bounded by a semaphore sized to the box
and the Anthropic account.

## Frequent-commit safety

Two layers, both cheap:

- **`hooks/stop_commit.sh`** — a harness Stop hook that runs `git add -A &&
  git commit` in the user's `workspace/` after every turn. Nothing is ever lost
  to a crash; worst case a resumed turn re-does its tail against committed state.
- **System-prompt discipline** — the mind is told to commit logically as it works
  (durable side effects), so history is meaningful, not one giant blob per turn.

## Voice

Azure Whisper for speech-to-text, an Azure neural voice for text-to-speech,
called inline from the surface or the mind server. No separate container.

## Deferred (multi-user machinery)

Real auth on the web surface, per-user rate limits/quotas, per-user process
pools, and an onboarding flow for unknown surface identities. The schema already
supports all of it; build when a second user is real.
