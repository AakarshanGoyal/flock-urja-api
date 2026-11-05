import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.portal.client import PortalClient

from .fake_portal import EMAIL, PASSWORD, FakePortal


async def _no_sleep(_seconds: float) -> None:
    return None


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        portal_base_url="https://portal.test",
        portal_email=EMAIL,
        portal_password=PASSWORD,
        db_path=str(tmp_path / "test.sqlite3"),
        index_ttl_seconds=900,
        energy_ttl_seconds=600,
        portal_max_rps=0,  # no throttling in tests
        portal_timeout_seconds=5,
        portal_max_retries=3,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def portal():
    return FakePortal()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def portal_client(settings, portal):
    return PortalClient(settings, transport=portal.transport(), sleep=_no_sleep)


@pytest.fixture
def api(settings, portal_client):
    app = create_app(settings, client=portal_client, warm_index=False)
    with TestClient(app) as c:
        yield c
