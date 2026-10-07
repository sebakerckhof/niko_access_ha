"""Shared fixtures: canned Niko cloud replies."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

API = "https://api.guardingvision.com"
EU = "https://apiieu.guardingvision.com"
SERIAL = "X12345678"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def login_ok(api_domain: str = "apiieu.guardingvision.com") -> dict:
    return {
        "meta": {"code": 200, "message": "ok"},
        "loginSession": {"sessionId": "sess-1", "rfSessionId": "rf-1"},
        "loginUser": {"userId": "user-1", "areaId": 314, "username": "me"},
        "loginArea": {"apiDomain": api_domain, "areaId": 314},
    }


def devices_reply(online: int = 1) -> dict:
    return {
        "meta": {"code": 200},
        "page": {"offset": 0, "limit": 50, "totalResults": 1, "hasNext": False},
        "deviceInfos": [
            {
                "deviceSerial": SERIAL,
                "name": "External unit",
                "deviceType": "510-31001",
                "deviceCategory": "COMMON",
                "deviceSubCategory": "VIS",
                "version": "V2.0.0 build 230705",
                "channelNumber": 1,
                "status": online,
            }
        ],
        "connectionInfos": {SERIAL: {"localIp": "", "wanIp": "192.0.2.1"}},
        "statusInfos": {},
    }


def call_reply(status: int) -> dict:
    # Shape as returned by the real 510-31001 (2026-10-06 probe).
    data = {
        "apiId": 1,
        "callStatus": status,
        "verFlag": 1,
        "callerInfo": {
            "buildingNo": 1, "floorNo": 1, "zoneNo": 1, "unitNo": 1, "devNo": 0, "devType": 5,
        },
        "rc": 1,
    }
    return {"meta": {"code": 200}, "data": json.dumps(data)}


# Real locksParams of the 510-31001: only lockId 3 (system relay A) enabled.
LOCKS = [
    {"lockId": 1, "enable": False, "openDuration": 3, "lockName": "Door lock name A"},
    {"lockId": 2, "enable": False, "openDuration": 3, "lockName": "Door lock name B"},
    {"lockId": 3, "enable": True, "openDuration": 3, "lockName": "System name A"},
    {"lockId": 4, "enable": False, "openDuration": 3, "lockName": "System name B"},
]


def isapi_reply(data) -> AiohttpClientMockResponse:
    return AiohttpClientMockResponse(
        "POST", "", json={"resultCode": "0", "resultDes": "success", "data": json.dumps(data)}
    )


async def isapi_router(method, url, data):
    """Answer the ISAPI tunnel depending on the tunnelled request."""
    if isinstance(data, bytes):
        data = data.decode()
    sent = parse_qs(data) if isinstance(data, str) else {k: [v] for k, v in data.items()}
    transmission = sent["transmissionData"][0]
    if transmission.startswith("GET /ISAPI/Custom/VideoIntercom/locksParams"):
        return isapi_reply(LOCKS)
    return isapi_reply({"statusCode": 1, "statusString": "OK", "subStatusCode": "ok"})


@pytest.fixture
def cloud(aioclient_mock):
    """Happy-path cloud: redirect to EU, the door station only, idle."""
    aioclient_mock.post(f"{API}/v3/users/login/v2", json=login_ok())
    aioclient_mock.post(f"{EU}/v3/users/login/v2", json=login_ok())
    aioclient_mock.get(f"{EU}/v3/userdevices/v1/devices/pagelist", json=devices_reply())
    aioclient_mock.get(f"{EU}/v3/devconfig/v1/call/{SERIAL}/status", json=call_reply(1))
    aioclient_mock.post(f"{EU}/api/device/isapi", side_effect=isapi_router)
    return aioclient_mock
