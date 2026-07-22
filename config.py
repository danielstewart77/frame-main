"""Non-secret settings, resolved from the environment with sane defaults.

Every value that must differ between the offline box and the box wired to the
proxy is an env var, so going live is a `.env` edit rather than a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: os.getenv("FRAME_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("FRAME_PORT", "8500")))

    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("FRAME_DB", str(ROOT / "db" / "registry.db")))
    )
    users_root: Path = field(
        default_factory=lambda: Path(os.getenv("FRAME_USERS_ROOT", str(ROOT / "users")))
    )

    # Sandbox. `fake` provisions nothing and is what the offline box runs.
    provisioner: str = field(default_factory=lambda: os.getenv("FRAME_PROVISIONER", "fake"))
    sandbox_image: str = field(
        default_factory=lambda: os.getenv("FRAME_SANDBOX_IMAGE", "frame-main-sandbox:latest")
    )
    app_port_range: tuple[int, int] = field(
        default_factory=lambda: (
            int(os.getenv("FRAME_APP_PORT_MIN", "9600")),
            int(os.getenv("FRAME_APP_PORT_MAX", "9699")),
        )
    )
    max_concurrent_sessions: int = field(
        default_factory=lambda: int(os.getenv("FRAME_MAX_CONCURRENT", "12"))
    )
    idle_timeout_minutes: int = field(
        default_factory=lambda: int(os.getenv("FRAME_IDLE_TIMEOUT_MINUTES", "30"))
    )
    # How often the reaper sweeps for idle containers. Well under the idle
    # timeout, since a session is only reclaimed on the first sweep past it.
    reap_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("FRAME_REAP_INTERVAL_SECONDS", "60"))
    )
    # Ceiling on a single turn. The harness retries an unreachable provider ten
    # times before giving up, so without this a bad endpoint wedges the frame.
    turn_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("FRAME_TURN_TIMEOUT_SECONDS", "1800"))
    )

    # Voice. `fake` needs no Azure reachability; `azure` is the Monday flip.
    voice: str = field(default_factory=lambda: os.getenv("FRAME_VOICE", "fake"))
    azure_speech_endpoint: str = field(
        default_factory=lambda: os.getenv("AZURE_SPEECH_ENDPOINT", "")
    )
    azure_speech_key: str = field(default_factory=lambda: os.getenv("AZURE_SPEECH_KEY", ""))
    azure_speech_region: str = field(default_factory=lambda: os.getenv("AZURE_SPEECH_REGION", ""))
    azure_speech_voice: str = field(
        default_factory=lambda: os.getenv("AZURE_SPEECH_VOICE", "en-US-AndrewMultilingualNeural")
    )
    azure_whisper_deployment: str = field(
        default_factory=lambda: os.getenv("AZURE_WHISPER_DEPLOYMENT", "whisper")
    )

    # Channel. The URL is what the container's shim calls back on, so it is the
    # host as seen from inside the sandbox, not the control plane's own bind.
    # The bearer is no longer a config value: it is minted per session at spawn
    # (see SessionManager.ensure_running), so a container speaks only for itself.
    channel_url: str = field(
        default_factory=lambda: os.getenv("FRAME_CHANNEL_URL", "http://host.docker.internal:8500")
    )
    channel_config_path: str = field(
        default_factory=lambda: os.getenv("FRAME_CHANNEL_CONFIG", "/opt/frame/mcp.json")
    )

    # Proxy the harnesses reach the provider through, handed to the container at
    # spawn time. One base URL and one token serve both harnesses; the token is
    # named for the proxy, not the provider, so it is not mistaken for an
    # Anthropic key. `_spawn_env` maps them onto the env var names each harness
    # actually reads (ANTHROPIC_* for claude, OPENAI_* for codex).
    anthropic_base_url: str = field(default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL", ""))
    ulmaiproxy_auth_token: str = field(
        default_factory=lambda: os.getenv("ULMAIPROXY_AUTH_TOKEN", "")
    )

    default_harness: str = field(default_factory=lambda: os.getenv("FRAME_DEFAULT_HARNESS", "claude"))
    default_model: str = field(default_factory=lambda: os.getenv("FRAME_DEFAULT_MODEL", "opus"))

    # Agent skills, shared read-only into every container. The two repos are
    # cloned/pulled once to skills_root by the control plane (admin "sync"
    # button) using the host account's git credential — never baked into the
    # image, never handed the PAT to a container. `<root>/claude-skills` mounts
    # at ~/.claude/skills, `<root>/codex-skills` at ~/.codex/skills.
    skills_root: Path = field(
        default_factory=lambda: Path(os.getenv("FRAME_SKILLS_ROOT", str(ROOT / "skills")))
    )
    claude_skills_repo: str = field(
        default_factory=lambda: os.getenv("FRAME_CLAUDE_SKILLS_REPO", "")
    )
    codex_skills_repo: str = field(
        default_factory=lambda: os.getenv("FRAME_CODEX_SKILLS_REPO", "")
    )

    server_url: str = field(
        default_factory=lambda: os.getenv("FRAME_SERVER_URL", "http://127.0.0.1:8500")
    )

    # How often the Telegram supervisor reconciles running pollers against the
    # `telegram_bots` table — starting one for a new bot, restarting one whose
    # token changed, stopping one whose row was removed.
    telegram_reconcile_seconds: int = field(
        default_factory=lambda: int(os.getenv("FRAME_TELEGRAM_RECONCILE_SECONDS", "15"))
    )

    # The operator/registration credential. It mints and lists users, resolves a
    # chat identity to an account, and can register further accounts once the box
    # is claimed — the fleet-admin authority, not any one user's. Empty means no
    # such principal exists and only console logins work. Set it in `.env` in any
    # real deployment.
    service_token: str = field(default_factory=lambda: os.getenv("FRAME_SERVICE_TOKEN", ""))
    # How long a console login stays valid before it must be re-entered.
    auth_token_ttl_hours: int = field(
        default_factory=lambda: int(os.getenv("FRAME_AUTH_TOKEN_TTL_HOURS", "720"))
    )

    debug: bool = field(default_factory=lambda: _bool("FRAME_DEBUG", False))


def load_dotenv(path: Path | None = None) -> None:
    """Fold `.env` into os.environ. Real env vars always win."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load() -> Settings:
    """Read settings fresh from the environment, with `.env` as the base layer."""
    load_dotenv()
    return Settings()
