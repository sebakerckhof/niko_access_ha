"""API client tests against canned cloud replies."""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.niko_access import api

from .conftest import API, EU, SERIAL, devices_reply, login_ok


def make_client(hass):
    return api.NikoAccessClient(
        async_get_clientsession(hass), "me@example.com", api.hash_password("pw"), "feat"
    )


def form(call) -> dict[str, str]:
    """aioclient_mock stores (method, url, data, headers)."""
    data = call[2]
    if isinstance(data, bytes):
        data = data.decode()
    if isinstance(data, str):
        return {k: v[0] for k, v in parse_qs(data).items()}
    return dict(data)


async def test_login_follows_redirect(hass, aioclient_mock):
    aioclient_mock.post(
        f"{API}/v3/users/login/v2",
        json={
            "meta": {"code": 1100},
            "loginArea": {"apiDomain": "apiieu.guardingvision.com"},
        },
    )
    aioclient_mock.post(f"{EU}/v3/users/login/v2", json=login_ok())
    client = make_client(hass)
    await client.login()

    assert client.api_url == EU
    assert client.session_id == "sess-1"
    assert client.user_id == "user-1"
    first, second = aioclient_mock.mock_calls
    assert form(first)["password"] == api.hash_password("pw")
    assert form(first)["redirect"] == ""
    assert form(second)["redirect"] == "1"
    headers = first[3]
    assert headers["clientType"] == "378"
    assert headers["appId"] == "NIKO"


@pytest.mark.parametrize(
    ("code", "exc"),
    [
        (1014, api.NikoAccessAuthError),
        (1013, api.NikoAccessAuthError),
        (1015, api.NikoAccessVerificationRequired),
        (500, api.NikoAccessError),
    ],
)
async def test_login_errors(hass, aioclient_mock, code, exc):
    aioclient_mock.post(f"{API}/v3/users/login/v2", json={"meta": {"code": code, "message": "x"}})
    with pytest.raises(exc):
        await make_client(hass).login()


async def test_devices_and_call_status(hass, cloud):
    client = make_client(hass)
    devices = await client.get_devices()
    assert [d.serial for d in devices] == [SERIAL]
    assert devices[0].online
    assert devices[0].device_type == "510-31001"

    call = await client.get_call_status(SERIAL)
    assert call.state == "idle"
    assert not call.ringing


async def test_unlock_sends_isapi(hass, cloud):
    client = make_client(hass)
    await client.unlock(SERIAL, 3)
    isapi_call = next(c for c in cloud.mock_calls if str(c[1]).endswith("/api/device/isapi"))
    sent = form(isapi_call)
    assert sent["subSerial"] == SERIAL
    assert sent["cmdId"] == "19713"
    assert sent["sessionId"] == "sess-1"
    assert sent["transmissionData"] == (
        'PUT /ISAPI/Custom/VideoIntercom/unlock?format=json\r\n{"lockId":3}'
    )


async def test_unlock_device_refusal(hass, aioclient_mock):
    aioclient_mock.post(f"{API}/v3/users/login/v2", json=login_ok(API))
    aioclient_mock.post(
        f"{API}/api/device/isapi",
        json={"resultCode": "0", "data": '{"statusCode":4,"subStatusCode":"invalidOperation"}'},
    )
    with pytest.raises(api.NikoAccessError, match="invalidOperation"):
        await make_client(hass).unlock(SERIAL, 5)


async def test_session_expiry_triggers_relogin(hass, aioclient_mock):
    aioclient_mock.post(f"{API}/v3/users/login/v2", json=login_ok(API))
    aioclient_mock.get(
        f"{API}/v3/userdevices/v1/devices/pagelist",
        json={"meta": {"code": 1002, "message": "session expired"}},
    )
    client = make_client(hass)
    with pytest.raises(api.NikoAccessError):
        await client.get_devices()
    logins = [c for c in aioclient_mock.mock_calls if "login" in str(c[1])]
    # initial login + one re-login, then gives up
    assert len(logins) == 2


async def test_pagination(hass, aioclient_mock):
    aioclient_mock.post(f"{API}/v3/users/login/v2", json=login_ok(API))
    page1 = devices_reply()
    page1["page"]["hasNext"] = True
    page2 = devices_reply()
    page2["deviceInfos"][0]["deviceSerial"] = "OTHER"
    aioclient_mock.get(
        f"{API}/v3/userdevices/v1/devices/pagelist", params={"offset": "0"}, json=page1
    )
    aioclient_mock.get(
        f"{API}/v3/userdevices/v1/devices/pagelist", params={"offset": "1"}, json=page2
    )
    devices = await make_client(hass).get_devices()
    assert [d.serial for d in devices] == [SERIAL, "OTHER"]


async def test_list_locks(hass, cloud):
    locks = await make_client(hass).list_locks(SERIAL)
    assert [(lk.lock_id, lk.enabled) for lk in locks] == [
        (1, False), (2, False), (3, True), (4, False)
    ]
    assert locks[2].name == "System name A"
