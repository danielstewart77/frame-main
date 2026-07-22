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


SERVICE_TOKEN = "test-service-token"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "registry.db",
        users_root=tmp_path / "users",
        provisioner="fake",
        voice="fake",
        max_concurrent_sessions=4,
        idle_timeout_minutes=30,
        service_token=SERVICE_TOKEN,
        # Pin proxy settings empty so tests are hermetic — otherwise they inherit
        # a real ANTHROPIC_BASE_URL from the box's env and reach out to the proxy.
        anthropic_base_url="",
        ulmaiproxy_auth_token="",
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
def app(settings, registry, provisioner, voice):
    from server import create_app

    return create_app(
        settings=settings, registry=registry, provisioner=provisioner, voice=voice
    )


@pytest.fixture
def client(app):
    """Authenticated as the service principal — acts for any user.

    Most routes require a caller now, and a surface (a bot) is exactly a service
    principal, so this mirrors how the control plane is actually driven. Tests
    that care about user-vs-user scoping use `logged_in` instead.
    """
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"
        yield test_client


@pytest.fixture
def anon_client(app):
    """No credentials — for asserting that a route refuses an unauthenticated caller."""
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def logged_in(app):
    """A browser-shaped client that registered and logged in as a real user.

    Carries the auth cookie the login sets, exactly as the console does, so it
    is scoped to its own account and nothing else. Exposes `.user_id`.
    """
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        test_client.post(
            "/auth/register",
            json={"username": "daniel", "password": "correct horse battery"},
        ).raise_for_status()
        body = test_client.post(
            "/auth/login",
            json={"username": "daniel", "password": "correct horse battery"},
        )
        body.raise_for_status()
        test_client.user_id = body.json()["user_id"]
        yield test_client


@pytest.fixture
def user(registry):
    return registry.create_user("Daniel")
