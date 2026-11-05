import pytest

from app.portal.client import MeterNotFound, PortalAuthError, PortalClient, PortalUnavailable, sign_request

from .conftest import _no_sleep, make_settings


@pytest.mark.anyio
async def test_logs_in_lazily_and_reads_session_expiry(portal_client, portal):
    data = await portal_client.search_meters("J10000", 1)
    assert data["total"] == 10
    assert portal.logins == 1
    assert portal_client.session_expires_at is not None


@pytest.mark.anyio
async def test_relogs_in_when_session_expires(portal_client, portal):
    await portal_client.search_meters()
    portal.expire_all_sessions()
    await portal_client.search_meters()  # 401 -> login -> retry
    assert portal.logins == 2


@pytest.mark.anyio
async def test_relogs_in_on_sveltekit_redirect(portal_client, portal):
    await portal_client.search_meters()
    portal.expire_all_sessions()
    page = await portal_client.meter_page("J100000")
    assert page.kind == "data" and portal.logins == 2


@pytest.mark.anyio
async def test_bad_credentials_raise_auth_error(tmp_path, portal):
    client = PortalClient(make_settings(tmp_path, portal_password="wrong"), transport=portal.transport(), sleep=_no_sleep)
    with pytest.raises(PortalAuthError):
        await client.search_meters()


@pytest.mark.anyio
async def test_retries_through_rate_limiting(portal_client, portal):
    await portal_client.search_meters()
    portal.rate_limit_next = 2
    data = await portal_client.list_transformers(1)
    assert data["total"] == 40
    assert portal_client.stats["rate_limited"] == 2


@pytest.mark.anyio
async def test_gives_up_after_max_retries(portal_client, portal):
    await portal_client.search_meters()
    portal.fail_next_5xx = 10
    with pytest.raises(PortalUnavailable):
        await portal_client.list_transformers(1)


@pytest.mark.anyio
async def test_signed_export_returns_full_dataset(portal_client, portal):
    data = await portal_client.export_all()
    assert data["total"] == len(portal.export)


@pytest.mark.anyio
async def test_export_refreshes_secret_when_signature_rejected(portal_client, portal):
    portal_client._signing_secret = "stale-secret"
    await portal_client.ensure_session()
    data = await portal_client.export_all()
    assert data["total"] == len(portal.export)


@pytest.mark.anyio
async def test_missing_meter(portal_client):
    with pytest.raises(MeterNotFound):
        await portal_client.meter_page("J999999")
    with pytest.raises(MeterNotFound):
        await portal_client.meter_energy("J999999")


def test_signature_matches_portal_algorithm():
    # Known answer computed by running the portal's own signer (crypto.subtle) in the browser.
    sig = sign_request("GET", "/portal/export", "page=1", "secret", "1700000000")
    assert sig == "81340cd1e31e597cb066514f51f103416827265923e80f700aefb1b64dc6a3b9"
