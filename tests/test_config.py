"""`.env` is the documented config surface — prove it actually reaches Settings."""

import os

from config import Settings, load_dotenv


def test_dotenv_values_reach_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "FRAME_PROVISIONER=docker\n"
        "ANTHROPIC_BASE_URL=http://host.docker.internal:8899\n"
        'ANTHROPIC_AUTH_TOKEN="hmp-test"\n',
        encoding="utf-8",
    )
    for key in ("FRAME_PROVISIONER", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    load_dotenv(env_file)
    settings = Settings()

    assert settings.provisioner == "docker"
    assert settings.anthropic_base_url == "http://host.docker.internal:8899"
    assert settings.anthropic_auth_token == "hmp-test"


def test_real_env_wins_over_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FRAME_PROVISIONER=docker\n", encoding="utf-8")
    monkeypatch.setenv("FRAME_PROVISIONER", "fake")

    load_dotenv(env_file)

    assert os.environ["FRAME_PROVISIONER"] == "fake"


def test_missing_dotenv_is_not_an_error(tmp_path):
    load_dotenv(tmp_path / "nope.env")
