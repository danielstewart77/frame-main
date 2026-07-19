# VPN cutover

Everything except live Azure voice and live provider inference is built and
tested against fakes; container provisioning and git durability are covered by
real-Docker integration tests. On the VPN, the cutover is configuration.

## 1. Build the sandbox image

```bash
docker build -f sandbox/Dockerfile -t frame-main-sandbox:latest .
```

Once the image exists, `tests/test_docker_integration.py` stops skipping and
proves the real path: a pristine container clones its session branch off the
mounted bare repo, both harness CLIs are installed, the Stop hook pushes work
back to the host, and that work survives the container being destroyed.

## 2. Flip the provisioner

```
FRAME_PROVISIONER=docker
ANTHROPIC_BASE_URL=<proxy base url>
ANTHROPIC_AUTH_TOKEN=<proxy token>
```

`FakeProvisioner` and `DockerProvisioner` satisfy the same interface, so nothing
above `sandbox/provision.py` changes. Verify with:

```bash
curl -s localhost:8500/health
curl -sX POST localhost:8500/identities \
     -d '{"surface":"telegram","external_id":"1"}' -H 'content-type: application/json'
curl -sX POST localhost:8500/users/<user_id>/sessions -d '{}' -H 'content-type: application/json'
curl -sN -X POST localhost:8500/sessions/<session_id>/turn \
     -d '{"prompt":"print hello then commit"}' -H 'content-type: application/json'
```

A real turn should emit a `session` event carrying the harness's own id, stream
`text` and `tool` events, and end with `result`. If the proxy is unreachable you
will instead see repeated `status` events reading "retrying provider (n/10)",
then an `error` — that pattern means the endpoint or token is wrong, not that
the control plane is broken.

Then confirm the Stop hook pushed:

```bash
git --git-dir=users/<user_id>/origin.git branch
curl -s localhost:8500/sessions/<session_id>/diff
```

## 3. Flip voice

```
FRAME_VOICE=azure
AZURE_SPEECH_ENDPOINT=https://<resource>.openai.azure.com
AZURE_SPEECH_KEY=<key>
AZURE_SPEECH_REGION=<region>
AZURE_WHISPER_DEPLOYMENT=<deployment name>
```

`AzureVoice` and `FakeVoice` are interchangeable at the call site. Verify:

```bash
curl -sX POST localhost:8500/voice/speak -H 'content-type: application/json' \
     -d '{"text":"cutover check"}' -o /tmp/tts.mp3 && file /tmp/tts.mp3
curl -sX POST localhost:8500/voice/transcribe -F file=@/tmp/tts.mp3
```

## 4. Start the Telegram surface

```
TELEGRAM_BOT_TOKEN=<token>
```

```bash
python surfaces/telegram-bot.py
```

`/new`, then a plain message, should stream a reply into a single edited
message. The bot holds no attachment state of its own — it reads
`surface_bindings` through the API, so restarting it mid-conversation is safe.

## Known gaps at cutover

- **Web console** is designed but not built; the API it needs (`/users/{id}/frames`,
  per-session `frame_state`/`speaker`, `/surfaces/{surface}/{id}/layout`,
  `/sessions/{id}/diff`, `/sessions/{id}/stream`) is built and tested.
- **Live-app reverse proxy**: `app_port` is allocated and published on the
  container, but nothing yet fronts it at a per-session URL.
- **Interactive TUI pane** (pty over WebSocket) is not implemented; the
  read-only event stream is.
