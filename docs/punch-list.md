# Punch list — consolidated outstanding work

The single tracker for everything deferred while getting frame-main running
against the UT proxy. Collected from `go-live.md`, `metrics-roadmap.md`, and the
build notes so nothing gets lost. As of 2026-07-22.

## Current state (for context)

Running on `UlhGpuHamster` (`10.201.51.171`), bound `0.0.0.0:8500`, provisioner
`docker`, voice `fake`. The control plane runs as the `daniel` account. Sessions
reach Claude/Codex through `ulmaiproxy.utsystem.edu` on a box-wide token (or a
user's own key when set). Branch `user-management` holds the unmerged work.

## Done this cycle
- [x] Proxy wiring + rename (`ULMAIPROXY_AUTH_TOKEN`), containers run as the host user, `IS_SANDBOX`.
- [x] User management: admin role, self-service password, admin create/reset/enable-disable/role, forced-change flow.
- [x] Model picker reads live from the proxy (per caller's key).
- [x] Per-user proxy API key (falls back to the box-wide token).
- [x] Shared agent skills mounted read-only into every session (admin "sync" button); verified live.

## Sequence agreed
1. **Push `user-management` branch** (backup only, no PR yet).
2. **Test working on a real story** end to end (the acceptance gate).
3. Fold in any fixes the story test surfaces.
4. **Open the PR** once green.
5. **Switch to a service account** (below) once behavior is locked.

## Before we call it production

- [ ] **Service account + systemd.** Stop running as `daniel`. Create a non-login
      account (à la the proxy's `ulands`), in the `docker` group (no sudo), owning
      the DB, `users/`, and `skills/`. Move the git credential (`~/.git-credentials`
      with the skills PAT) to it. Run frame-main under a systemd unit. Code is
      already account-agnostic (uses the process uid + configurable paths), so this
      is `chown` + env + unit, no code.
- [ ] **Public HTTPS front door.** frame-main is on an internal `10.x` with no public
      name. A TLS-terminating reverse proxy (the pattern the AI proxy uses with Caddy)
      is needed for: remote console access beyond the LAN, ADO webhooks reaching us,
      and a genuinely `Secure` auth cookie. See `go-live.md` "bind address" note.
- [ ] **Verify Codex end to end.** Only Claude has been proven through the proxy. The
      `OPENAI_*` env vars are injected but a real `codex` turn (and its skills at
      `~/.codex/skills`) is unproven.
- [ ] **Live voice cutover (if in scope).** `FRAME_VOICE=azure` + Azure Speech creds
      are wired but untested live (`go-live.md` step 4).
- [ ] **Live Telegram test.** Per-user bot path is built and unit-tested but never
      exercised against a real BotFather token.

## Deferred features (roadmap)

- [ ] **Metrics & telemetry** — both buckets in `metrics-roadmap.md`: agent usage
      (tokens/cost per turn, retention prune) and software-delivery metrics (stories
      queued/completed, cycle time, LOC, PR throughput). Not built.
- [ ] **ADO webhook receiver** (`POST /webhooks/ado`) — the delivery-metrics pipe;
      depends on the public HTTPS front door.
- [ ] **Scheduled skills sync.** Today sync is the manual admin button only. A cron or
      control-plane periodic `git pull` would keep the shared clones fresh.
- [ ] **Per-user proxy base URL.** Only the *key* is per-user; the base URL is global.
      Extend if a user ever needs a different endpoint.
- [ ] **User deletion.** Admin can disable but not delete an account; no `DELETE
      /users/{id}` route yet.

## Hardening / hygiene

- [ ] **Encrypt secrets at rest.** Telegram bot tokens and per-user proxy keys are
      stored plaintext in the DB (same as the proxy does today). Consider encryption
      or a secret store.
- [ ] **Skills PAT scope/rotation.** The clone PAT (Code: Read) lives in the service
      account's `~/.git-credentials`; document rotation and least-privilege.
- [ ] **Automated skills integration test.** The read-only skills mount was verified
      live by hand; add a real-container integration test so it can't regress.

## Pointers
- Going live / cutover steps: `docs/go-live.md`
- Metrics intent + data sources: `docs/metrics-roadmap.md`
