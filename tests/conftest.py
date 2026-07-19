import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import registry as registry_mod  # noqa: E402
import voice as voice_mod  # noqa: E402
from config import Settings  # noqa: E402
from sandbox.provision import FakeProvisioner  # noqa: E402
from sessions import SessionManager  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "registry.db",
        users_root=tmp_path / "users",
        provisioner="fake",
        voice="fake",
        max_concurrent_sessions=4,
        idle_timeout_minutes=30,
    )


@pytest.fixture
def registry(settings):
    reg = registry_mod.Registry(settings.db_path)
    yield reg
    reg.close()


@pytest.fixture
def provisioner():
    return FakeProvisioner()


@pytest.fixture
def manager(registry, settings, provisioner):
    return SessionManager(registry, settings, provisioner)


@pytest.fixture
def voice():
    return voice_mod.FakeVoice()


@pytest.fixture
def client(settings, registry, provisioner, voice):
    from fastapi.testclient import TestClient

    from server import create_app

    app = create_app(settings=settings, registry=registry, provisioner=provisioner, voice=voice)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user(registry):
    return registry.create_user("Daniel")
