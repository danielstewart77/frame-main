-- frame-main registry: users + sessions. Multi-user schema from day one.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  user_id      TEXT PRIMARY KEY,        -- uuid
  display_name TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active'
);

-- surface identity -> user (a Telegram chat, a web login, etc.)
CREATE TABLE IF NOT EXISTS identities (
  surface      TEXT NOT NULL,           -- 'telegram' | 'web'
  external_id  TEXT NOT NULL,           -- telegram chat_id, web user id
  user_id      TEXT NOT NULL REFERENCES users(user_id),
  PRIMARY KEY (surface, external_id)
);

-- one row per task/topic session; the session owns its harness + model + container
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,        -- our stable session uuid
  user_id      TEXT NOT NULL REFERENCES users(user_id),
  title        TEXT,                    -- topic/task label, editable
  color        TEXT,                    -- accent color for the frame + session list, editable
  harness      TEXT NOT NULL,           -- 'claude' | 'codex' | ...
  model        TEXT NOT NULL,           -- e.g. 'opus'
  branch       TEXT NOT NULL,           -- git branch in origin.git for this session
  container_id TEXT,                    -- docker container (null when not running)
  app_port     INTEGER,                 -- reverse-proxied port for the live app (nullable)
  resume_id    TEXT,                    -- harness --resume id (null until first turn)
  transcript   TEXT,                    -- path to harness rollout jsonl (host-mounted)
  status       TEXT NOT NULL DEFAULT 'active',  -- active | done | archived
  outcome      TEXT,                    -- null while working; ok | error once a turn lands
  frame_state  TEXT NOT NULL DEFAULT 'closed',  -- closed | docked | minimized
  speaker      INTEGER NOT NULL DEFAULT 0,      -- per-frame spoken playback toggle
  created_at   TEXT NOT NULL,
  last_active  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id, status, last_active);

-- What a session actually said, durable past the process that heard it.
-- The bus fans events out live to whoever is attached, but nobody is attached
-- to a session running unattended, and its replay buffer is a bounded deque in
-- memory. This table is the record you read in the morning.
--
-- Text deltas are coalesced into one row per contiguous run: the harness emits
-- a token at a time, and a row per token would be a row per token.
CREATE TABLE IF NOT EXISTS session_events (
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,        -- bus seq; ordering within the session
  kind        TEXT NOT NULL,           -- text | tool | status | result | error | ...
  text        TEXT,                    -- the human-readable payload, when there is one
  data        TEXT,                    -- json: everything else on the event
  created_at  TEXT NOT NULL,
  PRIMARY KEY (session_id, seq)
);

-- a surface's "current" session is just a repointable pointer
CREATE TABLE IF NOT EXISTS surface_bindings (
  surface      TEXT NOT NULL,           -- 'telegram' | 'web'
  external_id  TEXT NOT NULL,           -- telegram chat_id
  session_id   TEXT NOT NULL REFERENCES sessions(id),
  PRIMARY KEY (surface, external_id)
);

-- per-surface console state that is not a property of any single session
CREATE TABLE IF NOT EXISTS surface_layout (
  surface           TEXT NOT NULL,
  external_id       TEXT NOT NULL,
  sidebar_collapsed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (surface, external_id)
);
