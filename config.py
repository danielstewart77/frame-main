"""Non-secret settings, resolved from the environment with sane defaults.

Every value that must differ between the offline box and the VPN box is an env
var, so Monday is a `.env` edit rather than a code change.
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

    # Harness credentials handed to the container at spawn time.
    anthropic_base_url: str = field(default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL", ""))
    anthropic_auth_token: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_AUTH_TOKEN", "")
    )

    default_harness: str = field(default_factory=lambda: os.getenv("FRAME_DEFAULT_HARNESS", "claude"))
    default_model: str = field(default_factory=lambda: os.getenv("FRAME_DEFAULT_MODEL", "opus"))

    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    server_url: str = field(
        default_factory=lambda: os.getenv("FRAME_SERVER_URL", "http://127.0.0.1:8500")
    )

    debug: bool = field(default_factory=lambda: _bool("FRAME_DEBUG", False))


def load() -> Settings:
    """Read settings fresh from the environment."""
    return Settings()
