"""Config flow, setup, entities and the ring event."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.niko_access.api import hash_password
from custom_components.niko_access.const import (
    CONF_API_URL,
    CONF_FEATURE_CODE,
    CONF_PASSWORD_MD5,
    DOMAIN,
)

from .conftest import API, EU, SERIAL, call_reply, devices_reply


def entry_for(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-1",
        title="me@example.com",
        data={
            CONF_USERNAME: "me@example.com",
            CONF_PASSWORD_MD5: hash_password("pw"),
            CONF_FEATURE_CODE: "feat",
            CONF_API_URL: EU,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_user_flow(hass, cloud, monkeypatch):
    # SADP auto-detects the door-station IP during setup.
    async def fake_find_ip(serial, duration=0):
        return "10.6.2.6"

    monkeypatch.setattr(
        "custom_components.niko_access.config_flow.async_find_ip", fake_find_ip
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: " me@example.com ", CONF_PASSWORD: "pw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "user-1"
    data = result["data"]
    assert data[CONF_USERNAME] == "me@example.com"
    assert data[CONF_PASSWORD_MD5] == hash_password("pw")
    assert CONF_PASSWORD not in data
    assert data[CONF_API_URL] == EU
    # Camera IP auto-detected and stored so the camera appears without Configure.
    assert result["result"].options.get("camera_ip") == "10.6.2.6"


async def test_user_flow_without_discovery(hass, cloud, monkeypatch):
    async def no_ip(serial, duration=0):
        return None

    monkeypatch.setattr(
        "custom_components.niko_access.config_flow.async_find_ip", no_ip
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "me@example.com", CONF_PASSWORD: "pw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options == {}  # no camera until IP known


async def test_user_flow_bad_password(hass, aioclient_mock):
    aioclient_mock.post(f"{API}/v3/users/login/v2", json={"meta": {"code": 1014}})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "me@example.com", CONF_PASSWORD: "nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_setup_entities_and_unlock(hass, cloud):
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("binary_sensor.external_unit_ringing").state == "off"
    assert hass.states.get("binary_sensor.external_unit_connectivity").state == "on"
    assert hass.states.get("sensor.external_unit_call_state").state == "idle"
    assert hass.states.get("event.external_unit_doorbell") is not None

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.external_unit_open_door_3"}, blocking=True
    )
    # Only the enabled lock (3) gets a button.
    buttons = [s.entity_id for s in hass.states.async_all("button")]
    assert buttons == ["button.external_unit_open_door_3"]
    unlocks = [
        c for c in cloud.mock_calls
        if str(c[1]).endswith("/api/device/isapi") and "unlock" in str(c[2])
    ]
    assert len(unlocks) == 1
    assert "%7B%22lockId%22%3A3%7D" in str(unlocks[0][2]) or '{"lockId":3}' in str(unlocks[0][2])

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_ring_fires_event_once(hass, cloud):
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    status_url = f"{EU}/v3/devconfig/v1/call/{SERIAL}/status"

    async def poll():
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
        await hass.async_block_till_done()

    cloud.clear_requests()
    cloud.get(status_url, json=call_reply(2))
    cloud.get(f"{EU}/v3/userdevices/v1/devices/pagelist", json=devices_reply())
    await poll()

    event_state = hass.states.get("event.external_unit_doorbell")
    assert event_state.attributes["event_type"] == "ring"
    first_fired = event_state.state
    assert hass.states.get("binary_sensor.external_unit_ringing").state == "on"
    assert hass.states.get("sensor.external_unit_call_state").state == "ringing"

    # Still ringing on the next poll: no second event.
    await poll()
    assert hass.states.get("event.external_unit_doorbell").state == first_fired


async def test_camera_created_when_ip_set(hass, cloud):
    entry = entry_for(hass)
    hass.config_entries.async_update_entry(
        entry, options={"camera_ip": "10.6.2.6", "camera_channel": 1, "camera_quality": "main"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Camera entity exists but its stream is lazy (not opened until viewed).
    cams = [s.entity_id for s in hass.states.async_all("camera")]
    assert cams == ["camera.external_unit_live_video"]


async def test_no_camera_without_ip(hass, cloud):
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_all("camera") == []
