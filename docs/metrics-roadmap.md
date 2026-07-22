# Metrics & telemetry — roadmap (not yet implemented)

This file records **what we want to track** and **where the data would come
from**, so we build it deliberately later rather than bolt it on. Nothing here
is implemented yet. It supersedes the earlier "usage tracking" plan by widening
the scope from token usage to software-delivery metrics as well.

## Why

Two kinds of visibility:
1. **Agent usage** — how much the agents cost and consume, for capacity and
   chargeback.
2. **Software delivery** — how the work itself flows, so the system can report
   on its own throughput (the agents are doing the development).

## Bucket 1 — Agent usage

Recorded per **turn**, aggregated by session / user / model / harness over a
time window:

- input / output / total tokens, and cache read/creation tokens
- estimated cost (tokens × per-model price)
- turn count, error rate, turn duration
- busiest models, per-user totals

**Source:** the harness stream already emits usage on its terminal `result`
event (Claude/Codex); capture it at turn completion. **Retention:** prune raw
rows past N days, keep rollups. (This is the piece we deferred — the capture
seam is `harness.py` → `sessions.turn()`.)

## Bucket 2 — Software delivery metrics

Illustrative, not an exact list — the intent is "normal software-development
tracking":

- **Stories queued** — backlog / work items not yet started
- **Stories completed** — items moved to Done over a period
- **Cycle time / lead time per story** — active→done, and created→done
- **Work in progress** — items in flight at once
- **Pull requests** — opened, merged, throughput over time
- **Review latency** — PR created → completed, time to first review
- **Lines of code** — added/removed per PR and per story
- **Commits per story, rework rate**

**Source:** Azure DevOps — **Boards** (work items) and **Repos** (pull
requests) — pushed to frame-main via **ADO Service Hooks (web hooks)** on
events such as `workitem.created` / `workitem.updated` and
`git.pullrequest.created` / `git.pullrequest.merged`. Lines of code come from
the PR/commit diffs (or `git` directly). A story's identifier links its work
item, its PRs, and its frame-main session so the three can be correlated.

## First integration step (being tested now)

An ADO web hook that POSTs to frame-main when a **pull request is created**.
This proves the delivery-metrics pipe end to end before we model or persist
anything. See the walkthrough in the chat / `docs/go-live.md` operational notes.
Requirements to keep in mind:

- The receiver URL must be **publicly reachable over HTTPS** — ADO Cloud
  (`dev.azure.com`) calls out from the internet and cannot reach frame-main's
  internal `10.x` address directly. Options: expose frame-main behind the same
  kind of TLS reverse proxy the AI proxy uses, tunnel it, or (for inspecting
  the payload only) point the hook at a throwaway receiver.
- frame-main does not yet have a webhook receiver endpoint; that is the next
  thing to add when we implement Bucket 2.

## Status

Not implemented. Wishlist to scope and build later, one bucket at a time.
