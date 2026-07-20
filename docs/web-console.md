# Web console

The console is the desktop control surface for sessions. It lives in
[`console/`](../console/) — one HTML shell, one stylesheet, one script. No
framework, no bundler, no build step: the agent-server serves the directory
statically and the page speaks only the HTTP API in
[DESIGN.md](../DESIGN.md#http-api).

It was ported from Spark to Bloom's terminal page
(`spark_to_bloom/src/templates/terminal.html`), which is where the streaming
loop, panel grid, TTS clip queue, mic dictation, pending-message queue, and
interrupt pill come from. That page remains the closest thing to a second
reference if a behaviour here is ever unclear.

Open it at `/console`.

## Identity

The console is a `web` surface like any other. The first `GET /console` sets an
httpOnly `frame_console_id` cookie; `GET /console/bootstrap` exchanges that
cookie for a user via the normal `resolve_user` path and returns everything the
page needs to restore itself — user id, sidebar state, open frames, and the
harness list.

## Layout lives on the server

Nothing is kept in `localStorage`. The open set and each frame's persistent
state are `frame_state` on the session row (`closed` / `docked` / `minimized`),
titles and accent colours are the `title` and `color` columns, and the sidebar's
collapsed state is on the surface binding. A reload, a different machine, or a
server restart all restore the same layout, because
`GET /users/{user_id}/frames` is the source of truth rather than the browser.

`maximized` is deliberately *not* persisted. It's a transient overlay: it hides
the other frames without touching their state, so maximizing and restoring is a
no-op on the underlying layout.

## Frames

Each frame is one session. It streams over `WS /sessions/{id}/stream` and
renders the normalised event vocabulary directly — `text` appends to the current
assistant line, `tool` and `status` render dim, `error` renders red, `result`
ends the turn.

Typed messages join a FIFO queue and render dimmed until the turn they belong to
starts producing events; while one is in flight it carries an interrupt pill
wired to `POST /sessions/{id}/interrupt`, which terminates the harness process
inside the container. Docked frames auto-tile at `ceil(sqrt(n))` columns.
Minimized frames collapse to a strip of tabs below the grid.

Per frame: a speaker toggle (spoken replies via `POST /voice/speak`, buffered
and flushed after a talking pause, played through a clip queue), hold-to-talk
dictation via the browser's `SpeechRecognition`, rename, and an accent colour
that tints the frame border, the dock tab, and the sidebar entry.

## Sidecar panes

The frame menu opens one pane at a time to the right of the conversation:

- **browser** — an iframe at `/sessions/{id}/app/`, the session's own app
  reverse-proxied off its `app_port`.
- **diff** — `GET /sessions/{id}/diff` for the session branch.
- **full terminal** — `WS /sessions/{id}/tui`, a real pty on the container.

## Known limits

These are real boundaries, not TODOs — they're the cost of having no build step
and no upload path, and each one fails visibly rather than silently.

- **The terminal pane is not a terminal emulator.** Escape sequences are
  stripped and output is appended. Shell commands and harness slash-commands
  work; a full-screen curses app will render wrong. Fixing it properly means
  vendoring xterm.js, which the offline box can't fetch from a CDN.
- **Drag-and-drop inlines text into the composer.** Dropped files under 512 KB
  are appended to the message; larger or binary files are refused with a visible
  line in the feed. There's no upload route, so nothing is written into the
  session workspace.
- **The browser pane can't help an app that hardcodes root-absolute URLs.** A
  `<base>` tag is injected into proxied HTML so relative assets resolve under
  the subpath, but a script that fetches `/api/x` will escape the prefix. See
  the module docstring in [`proxy.py`](../proxy.py).
