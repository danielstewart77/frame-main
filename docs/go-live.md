# Going live against the proxy

Out of the box the app runs fully offline: `FRAME_PROVISIONER=fake` and
`FRAME_VOICE=fake`, so the whole control plane — sessions, turns, streaming, git
durability, auth, surface bindings — is exercised end to end with no Docker
daemon, no provider account, and no network reachability. The offline suite and
the real-Docker integration tests both pass in that state.

Going live is configuration, not code. Sessions reach Claude and Codex through
the UT System AI proxy at **`ulmaiproxy.utsystem.edu`** instead of calling a
provider directly. One base URL and one token serve both harnesses: the control
plane maps them onto the env var names each harness actually reads — `ANTHROPIC_*`
for claude, `OPENAI_*` for codex — so a single proxy credential drives both.
`FakeProvisioner` and `DockerProvisioner` satisfy the same interface, so nothing
above `sandbox/provision.py` changes.

**Reachability.** The proxy is a network host reached by DNS name, so the box
running frame-main — and therefore its session containers, which get outbound
network on the default bridge — must be able to resolve and reach
`ulmaiproxy.utsystem.edu`. This is separate from `host.docker.internal`, which a
container uses only to call *back* to the control plane's channel
(`FRAME_CHANNEL_URL`), not to reach the proxy.

## 1. Build the sandbox image

```bash
docker build -f sandbox/Dockerfile -t frame-main-sandbox:latest .
```

Once the image exists, `tests/test_docker_integration.py` stops skipping and
proves the real path: a pristine container clones its session branch off the
mounted bare repo, both harness CLIs are installed, the Stop hook is declared
and pushes work back to the host, and that work survives the container being
destroyed. These tests cover provisioning and durability; they do **not** make a
live inference call — the first real turn through the proxy happens at step 3.

## 2. Point sessions at the proxy

In `.env`:

```
FRAME_PROVISIONER=docker
ANTHROPIC_BASE_URL=https://ulmaiproxy.utsystem.edu
ULMAIPROXY_AUTH_TOKEN=<proxy token>
```

`ANTHROPIC_BASE_URL` is the proxy base each harness appends its provider's API
path to. `ULMAIPROXY_AUTH_TOKEN` is the bearer the proxy expects. At spawn,
`sessions._spawn_env` injects the pair into the container under both harnesses'
env var names — `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` for claude and
`OPENAI_BASE_URL`/`OPENAI_API_KEY` for codex — so the one credential drives
whichever harness the session runs. Neither is baked into the image.

Confirm `FRAME_DEFAULT_MODEL` (and any model a session requests) is a name the
proxy accepts — if it routes by a specific model id or deployment name rather
than the `opus`/`sonnet` aliases, set it accordingly.

## 3. Verify a real turn

Every route but the health probe, the login/register endpoints, and the console
shell requires a bearer token, so claim the box and log in first. On a fresh box
`POST /auth/register` is open; after the first account it takes the service
token.

```bash
curl -s localhost:8500/health

# claim the box (open only while no account exists)
curl -sX POST localhost:8500/auth/register -H 'content-type: application/json' \
     -d '{"username":"op","password":"<password>","display_name":"Operator"}'

# log in; capture the token and your user id
curl -sX POST localhost:8500/auth/login -H 'content-type: application/json' \
     -d '{"username":"op","password":"<password>"}'
TOKEN=<token from the login response>
UID=<user_id from the login response>

curl -sX POST localhost:8500/users/$UID/sessions \
     -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{}'
curl -sN -X POST localhost:8500/sessions/<session_id>/turn \
     -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"prompt":"print hello then commit"}'
```

A real turn should emit a `session` event carrying the harness's own id, stream
`text` and `tool` events, and end with `result`. If the proxy is unreachable or
the token is wrong you will instead see repeated `status` events reading
"retrying provider (n/10)", then an `error`, bounded by
`FRAME_TURN_TIMEOUT_SECONDS`. That pattern means the endpoint or token is wrong,
not that the control plane is broken.

Then confirm the Stop hook pushed the work back to the host:

```bash
git --git-dir=users/$UID/origin.git branch
curl -s localhost:8500/sessions/<session_id>/diff -H "authorization: Bearer $TOKEN"
```

## 4. Voice (optional)

Voice is independent of inference and off by default (`FRAME_VOICE=fake`). It
uses Azure Whisper STT and an Azure neural voice for TTS, reached at their own
endpoint with their own key — set these only if voice is in scope for launch and
the box can reach them:

```
FRAME_VOICE=azure
AZURE_SPEECH_ENDPOINT=https://<resource>.openai.azure.com
AZURE_SPEECH_KEY=<key>
AZURE_SPEECH_REGION=<region>
AZURE_WHISPER_DEPLOYMENT=<deployment name>
```

`AzureVoice` and `FakeVoice` are interchangeable at the call site. Verify:

```bash
curl -sX POST localhost:8500/voice/speak -H "authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"text":"launch check"}' -o /tmp/tts.mp3 && file /tmp/tts.mp3
curl -sX POST localhost:8500/voice/transcribe -H "authorization: Bearer $TOKEN" \
     -F file=@/tmp/tts.mp3
```

## 5. Connect a Telegram bot (optional)

Telegram is per-user and supervised in-process — there is no separate surface to
start. Create a bot with BotFather, then paste its token on the console settings
screen (or `PUT /users/{id}/telegram` with `{"bot_token": "..."}`). The
supervisor picks it up on its next reconcile.

The first chat to message the bot is enrolled as its owner and locked in; every
other chat is ignored. `/new`, then a plain message, should stream a reply into a
single edited message. The poller holds no attachment state of its own — it
reads `surface_bindings` through the manager, so a restart mid-conversation is
safe.

## Operational notes

- **Service token.** Set `FRAME_SERVICE_TOKEN` (e.g. `openssl rand -base64 32`).
  Without it there is no operator/fleet principal and registration cannot be
  re-closed after the first account is claimed.
- **Bind address.** `FRAME_HOST=127.0.0.1` serves localhost only. To reach the
  console or API from another machine, bind `0.0.0.0` behind a TLS-terminating
  reverse proxy; the login cookie is `httponly`/`samesite=lax` but the app
  itself does not terminate TLS.
