# Web console — build spec

The console is not greenfield. A working reference implementation of most of it
already exists as the Spark to Bloom terminal page:

    /home/daniel/Storage/Dev/spark_to_bloom/src/templates/terminal.html

One 1038-line Jinja template, vanilla JS, no framework and no build step. Read
it before writing anything here. This document is the **delta** — what
frame-main's console needs that the reference page does not already do — plus
the handful of places the two deliberately disagree.

## Stack

Same as the reference: one server-rendered template, vanilla JS, no build step.
The console is served by the agent-server itself as a static template; it speaks
only the HTTP API in [DESIGN.md](../DESIGN.md#http-api). No React, no Vite, no
bundler. The reference page proves the hard parts (streaming, TTS, mic, panel
grid, reattach) work at this weight, and a build step buys nothing.

## Carried over as-is

These behaviours are specified *by the reference implementation*. Port them;
don't redesign them.

| Behaviour | Reference symbol |
|---|---|
| Per-session panel/frame creation | `createPanel` |
| Auto-tiling grid | `relayoutGrid` |
| Live event stream + normalised dispatch | `connectStream`, `handleEvent` |
| Assistant line buffering | `startAssistantLine`, `appendAssistantChunk` |
| Per-frame speaker toggle, TTS buffer + clip queue | `loadSpeakerPref`, `ttsAccumulate`, `flushTtsNow`, `playNextTtsClip` |
| Hold-to-talk mic dictation | `startMic`, `stopMic` |
| Rename + accent-colour swatches | `openRename`, `commitRename`, `buildSwatches` |
| Auto-growing composer | `autoGrowInput` |
| Mobile viewport lock (single frame, back to list) | `isWide` media query |

Two reference behaviours are **not** in `DESIGN.md` but should be carried
anyway, because they solve problems frame-main also has:

- **Pending-message queue with optimistic echo.** Typed messages queue and
  render dimmed until the harness echoes them back (`pendingQueue`,
  `trySendFront`, `queueFront`). Without it, a busy frame silently swallows
  input.
- **Interrupt pill.** An in-flight turn gets a cancel affordance
  (`makeInterruptPill`, `doInterrupt`).

`beginReattachLoop` / `pollForSuccessor` (handing a frame to a successor session
after rotation) has no frame-main equivalent — sessions here are the unit and
don't rotate. Do not port it.

## Delta — build these

1. **Spawn.** A plus button creating a session: pick harness + model, open a
   frame. `POST /users/{user_id}/sessions`. The reference page deliberately
   removed spawn; frame-main needs it.
2. **Session sidebar.** Left rail listing active sessions with an archived view,
   click to open or focus. Collapsible; collapsed state persists via
   `GET`/`PATCH /surfaces/web/{external_id}/layout`.
3. **Frame window management.** The `docked` / `minimized` persistent states
   plus transient `maximized`, per
   [DESIGN.md](../DESIGN.md#frame-window-management). The un-maximize rule is
   the part to get right: restoring from maximized returns every frame to its
   *own* persistent state, so maximize-then-restore is a no-op on the layout.
   Persistent state is written with `PATCH /sessions/{id}` `{frame_state}`.
4. **Drag-and-drop input.** Files and folders dropped on a frame, forwarded to
   that session's harness. Images too — the composer is multimodal.
5. **Sidecar panes.** Frame menu opens these to the right of the conversation:
   - **Diff** — `GET /sessions/{id}/diff`, rendered live.
   - **Browser** — an iframe at `/sessions/{id}/app/`, the session's own app
     reverse-proxied off its `app_port`.
   - **Full terminal** — xterm.js over `WS /sessions/{id}/tui`.
6. **Frame menu actions.** Archive, delete, pull-to-local instructions
   (`GET /sessions/{id}/clone-url`), and open-code — the repo is already cloned
   on the host, so this launches an editor view or hands off a local
   `code <path>` rather than re-cloning.

## Resolved conflict — layout persistence

The reference page keeps the open set and labels in `localStorage`
(`saveOpenSet`, `recalledOpenSet`, `saveLabels`). frame-main puts them
server-side: the open set is `frame_state` on the session row, titles and
colours are `title` and `color` columns, and the sidebar's collapsed state is on
the surface binding. Server-side wins — layout should follow you across devices,
and `GET /users/{user_id}/frames` exists precisely to restore it. Use
`localStorage` for nothing.

## Visual spec

There isn't one, and that's deliberate. Inherit the reference page's look; the
only geometry rule that matters is the one in `DESIGN.md` — docked frames
auto-tile and the tile size is a function of the docked count.
