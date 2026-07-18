# frame-main

A minimal, single-box agent system: one FastAPI control plane wraps agent
harnesses (`claude`, `codex`, ...) as sessions, one isolated mind per user, with
a web console and a Telegram surface. Each session runs in a pristine container;
work persists to per-user git repos.

See [DESIGN.md](DESIGN.md) for the full architecture.
