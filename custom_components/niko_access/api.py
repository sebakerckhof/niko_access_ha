"""Async client for the Niko Access cloud (a Hik-Connect OEM backend).

Reverse-engineered from the `be.niko.accesscontrol` Android app (v1.5.0). The app
is a re-skinned Hik-Connect that talks to Hikvision's OEM cloud
(`api.guardingvision.com`) with `clientType=378` and `appId=NIKO`.

Two API generations are used side by side:

* v3 endpoints (`/v3/...`) return `{"meta": {"code": 200, ...}, ...}` and take the
  session in a `sessionId` header.
* v2 endpoints (`/api/...`) return `{"resultCode": "0", ...}` and expect the
  session and client info as form fields.

Device control is tunnelled ISAPI: `POST /api/device/isapi` with
`transmissionData = "<METHOD> <path>\\r\\n<json body>"` (cmdId 19713). Ring state
comes from `GET /v3/devconfig/v1/call/{serial}/status`.

This module has no Home Assistant imports so it can be exercised standalone
for interoperability with hardware the user owns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.guardingvision.com"

CLIENT_TYPE = "378"
CUSTOM_NO = "3000078"
APP_ID = "NIKO"
CLIENT_VERSION = "1.5.0.1110"
OS_VERSION = "13"
# Base64 of "HomeAssistant"; shown in the account's terminal list.
CU_NAME = "SG9tZUFzc2lzdGFudA=="

ISAPI_CMD_ID = 19713

# meta.code values, from YSNetSDKException (app code = 100000 + meta.code) and
# the Niko LoginActivity error handling.
CODE_OK = 200
CODE_REDIRECT = 1100
# 1013 user not exist, 1014 wrong password, 1072/1073 account variants.
CODE_WRONG_CREDENTIALS = {1013, 1014, 1072, 1073}
# 1015 locked -> image captcha, 1011/1012 verify code, 1069 too many terminals,
# 6001/6002 terminal (hardware signature) binding.
CODE_NEEDS_VERIFICATION = {1011, 1012, 1015, 1069, 6001, 6002}
# Codes we treat as "session gone, log in again". Best effort; the app maps
# these through a generic session-error path.
CODE_SESSION_INVALID = {401, 1002, 1003, 99997}

CALL_STATUS_IDLE = 1
CALL_STATUS_RINGING = 2
CALL_STATUS_IN_CALL = 3

DEVICE_LIST_FILTER = "CONNECTION,STATUS,SWITCH,WIFI,NODISTURB,P2P,KMS,HIDDNS,CHANNEL,FEATURE"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Refresh the session proactively; the cloud's exact TTL is unknown.
SESSION_REFRESH_AFTER = 30 * 60


class NikoAccessError(Exception):
    """Generic API failure."""

    def __init__(self, message: str, code: int | str | None = None) -> None:
        super().__init__(message if code is None else f"{message} (code {code})")
        self.code = code


class NikoAccessAuthError(NikoAccessError):
    """Credentials rejected."""


class NikoAccessVerificationRequired(NikoAccessError):
    """The cloud wants an SMS / e-mail / captcha step we cannot do headless."""


class NikoAccessSessionError(NikoAccessError):
    """Session expired or invalid."""


@dataclass
class NikoDevice:
    """A device registered on the account (typically the indoor station)."""

    serial: str
    name: str
    device_type: str
    category: str | None
    version: str | None
    channels: int
    online: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class NikoLock:
    """One entry of /ISAPI/Custom/VideoIntercom/locksParams."""

    lock_id: int
    name: str
    enabled: bool


@dataclass
class CallStatus:
    """Doorbell call state for one device."""

    status: int | None
    caller: dict[str, Any] | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def ringing(self) -> bool:
        return self.status == CALL_STATUS_RINGING

    @property
    def state(self) -> str:
        return {
            CALL_STATUS_IDLE: "idle",
            CALL_STATUS_RINGING: "ringing",
            CALL_STATUS_IN_CALL: "in_call",
        }.get(self.status or -1, "unknown")


def hash_password(password: str) -> str:
    """The app sends MD5(password) as lowercase hex."""
    return hashlib.md5(password.encode()).hexdigest()  # noqa: S324 - protocol requirement


class NikoAccessClient:
    """Minimal Hik-Connect/Niko cloud client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        account: str,
        password_md5: str,
        feature_code: str,
        api_url: str | None = None,
        lang: str = "en-GB",
    ) -> None:
        self._http = session
        self._account = account
        self._password_md5 = password_md5
        self._feature_code = feature_code
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self._lang = lang
        self.session_id: str | None = None
        self.refresh_session_id: str | None = None
        self.area_id: int | None = None
        self.user_id: str | None = None
        self._session_at = 0.0
        self._sysconf: list[str] | None = None

    async def async_get_sysconf(self) -> list[str]:
        """System config array; index 15/16 hold the CAS server host/port.

        Needed to build the CAS token the local CPD7 stream authenticates with.
        """
        if self._sysconf is None:
            data = await self._v3("GET", "/v3/configurations/system/info")
            raw = (data.get("systemConfigInfo") or {}).get("sysConf", "")
            self._sysconf = str(raw).split("|")
        return self._sysconf

    async def get_ticket_info(self, serial: str, channel: int = 1) -> dict[str, Any]:
        """Stream ticket (the `ut.` session token) + key/sessionId for a channel.

        GET v3/cameras/ticketInfo -> {ticket, key, sessionId, ...}. The `ticket`
        is the cloud stream session token used as the `ssn` in tts://talk URLs.
        """
        data = await self._v3(
            "GET", "/v3/cameras/ticketInfo",
            params={"deviceSerial": serial, "channelNo": str(channel)},
        )
        return data.get("ticketInfo") or {}

    async def get_system_info(self) -> dict[str, Any]:
        """Full systemConfigInfo (TTS/STUN/VTM servers etc.) for talk setup."""
        data = await self._v3("GET", "/v3/configurations/system/info")
        return data.get("systemConfigInfo") or {}

    def cas_token(self) -> dict[str, Any]:
        """Token dict consumed by the vendored cpd7.cas.EzvizCAS client."""
        return {
            "session_id": self.session_id,
            "rf_session_id": self.refresh_session_id,
            "username": self.user_id,
            "api_url": self.api_url.replace("https://", "").replace("http://", ""),
            "feature_code": self._feature_code,
            "hardware_code": self._feature_code,
            "service_urls": {"sysConf": self._sysconf or []},
        }

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        """Log in, following the regional redirect if the cloud sends one."""
        data = await self._login_once(redirect="")
        if _meta_code(data) == CODE_REDIRECT:
            area = data.get("loginArea") or {}
            domain = area.get("apiDomain")
            if not domain:
                raise NikoAccessError("Login redirect without apiDomain", CODE_REDIRECT)
            self.api_url = _normalise_domain(domain)
            _LOGGER.debug("Login redirected to %s", self.api_url)
            data = await self._login_once(redirect="1")

        code = _meta_code(data)
        if code in CODE_WRONG_CREDENTIALS:
            raise NikoAccessAuthError(_meta_message(data), code)
        if code in CODE_NEEDS_VERIFICATION:
            raise NikoAccessVerificationRequired(_meta_message(data), code)
        if code != CODE_OK:
            raise NikoAccessError(f"Login failed: {_meta_message(data)}", code)

        session = data.get("loginSession") or {}
        self.session_id = session.get("sessionId")
        self.refresh_session_id = session.get("rfSessionId")
        if not self.session_id:
            raise NikoAccessError("Login succeeded but no sessionId returned")
        user = data.get("loginUser") or {}
        self.user_id = user.get("userId")
        self.area_id = user.get("areaId")
        area = data.get("loginArea") or {}
        if area.get("apiDomain"):
            self.api_url = _normalise_domain(area["apiDomain"])
        self._session_at = time.monotonic()

    async def _login_once(self, redirect: str) -> dict[str, Any]:
        form = {
            "account": self._account,
            "password": self._password_md5,
            "imageCode": "",
            "featureCode": self._feature_code,
            "smsCode": "",
            "cuName": CU_NAME,
            "longitude": "",
            "latitude": "",
            "bizType": "",
            "smsToken": "",
            "redirect": redirect,
        }
        return await self._raw("POST", "/v3/users/login/v2", form=form, auth=False)

    async def refresh(self) -> None:
        """Swap the refresh token for a new session; falls back to full login."""
        if not self.refresh_session_id:
            await self.login()
            return
        try:
            data = await self._raw(
                "PUT",
                "/v3/apigateway/login",
                form={
                    "refreshSessionId": self.refresh_session_id,
                    "featureCode": self._feature_code,
                    "cuName": CU_NAME,
                },
                auth=False,
            )
        except NikoAccessError:
            await self.login()
            return
        info = data.get("sessionInfo") or {}
        if _meta_code(data) != CODE_OK or not info.get("sessionId"):
            await self.login()
            return
        self.session_id = info["sessionId"]
        self.refresh_session_id = info.get("refreshSessionId") or self.refresh_session_id
        self._session_at = time.monotonic()

    async def _ensure_session(self) -> None:
        if not self.session_id:
            await self.login()
        elif time.monotonic() - self._session_at > SESSION_REFRESH_AFTER:
            await self.refresh()

    # --------------------------------------------------------------- devices

    async def get_devices(self) -> list[NikoDevice]:
        devices: list[NikoDevice] = []
        offset = 0
        while True:
            data = await self._v3(
                "GET",
                "/v3/userdevices/v1/devices/pagelist",
                params={
                    "groupId": "-1",
                    "limit": "50",
                    "offset": str(offset),
                    "filter": DEVICE_LIST_FILTER,
                },
            )
            infos = data.get("deviceInfos") or []
            connection = data.get("connectionInfos") or {}
            statuses = data.get("statusInfos") or {}
            for info in infos:
                serial = info.get("deviceSerial")
                if not serial:
                    continue
                devices.append(
                    NikoDevice(
                        serial=serial,
                        name=info.get("name") or serial,
                        device_type=info.get("deviceType") or "",
                        category=info.get("deviceCategory"),
                        version=info.get("version"),
                        channels=int(info.get("channelNumber") or 1),
                        online=int(info.get("status") or 0) == 1,
                        raw={
                            "device": info,
                            "connection": connection.get(serial),
                            "status": statuses.get(serial),
                        },
                    )
                )
            page = data.get("page") or {}
            if not page.get("hasNext") or not infos:
                break
            offset += len(infos)
        return devices

    async def get_call_status(self, serial: str) -> CallStatus:
        data = await self._v3("GET", f"/v3/devconfig/v1/call/{serial}/status")
        payload = _json_or_none(data.get("data")) or {}
        return CallStatus(
            status=payload.get("callStatus"),
            caller=payload.get("callerInfo"),
            raw=payload,
        )

    # ----------------------------------------------------------------- ISAPI

    async def isapi(
        self, serial: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """Tunnel an ISAPI request through the cloud; returns the parsed reply."""
        transmission = f"{method} {path}\r\n"
        if body is not None:
            transmission += json.dumps(body, separators=(",", ":"))
        data = await self._v2(
            "POST",
            "/api/device/isapi",
            form={
                "subSerial": serial,
                "cmdId": str(ISAPI_CMD_ID),
                "transmissionData": transmission,
            },
        )
        reply = data.get("data")
        parsed = _json_or_none(reply)
        return parsed if parsed is not None else reply

    async def get_pwd_reset_code(self, source: str, v2: bool = True) -> dict[str, Any]:
        """Ask the cloud for a device password-reset code (does NOT apply it).

        Mirrors the app's Hik-Connect "reset device password" screen:
        GET /v3/devconfig[/v2]/pwd/reset?source=<qr/serial>. Read-only probe.
        """
        path = "/v3/devconfig/v2/pwd/reset" if v2 else "/v3/devconfig/pwd/reset"
        return await self._v3("GET", path, params={"source": source})

    async def get_alarms(self, **params: str) -> list[dict[str, Any]]:
        """Recent alarm/event messages (may carry a snapshot in picUrl).

        Query params are not pinned down yet: the app passes limit, queryType
        and lastTime to /v3/alarms, or a device/time window to /advanced.
        """
        path = "/v3/alarms/advanced" if "deviceSerial" in params else "/v3/alarms"
        data = await self._v3("GET", path, params={"limit": "20", **params})
        return data.get("alarms") or []

    async def get_locks(self, serial: str) -> Any:
        return await self.isapi(
            serial, "GET", "/ISAPI/Custom/VideoIntercom/locksParams?format=json"
        )

    async def list_locks(self, serial: str) -> list[NikoLock]:
        """Parsed locksParams. On the 510-31001 this is a bare list of four slots
        (lockId 1-4: door locks A/B on the station, system relays A/B)."""
        reply = await self.get_locks(serial)
        if isinstance(reply, dict):
            reply = next((v for v in reply.values() if isinstance(v, list)), [])
        locks = []
        for item in reply if isinstance(reply, list) else []:
            if isinstance(item, dict) and "lockId" in item:
                locks.append(
                    NikoLock(
                        lock_id=int(item["lockId"]),
                        name=item.get("lockName") or f"Lock {item['lockId']}",
                        enabled=bool(item.get("enable", True)),
                    )
                )
        return locks

    async def get_caller_info(self, serial: str) -> Any:
        return await self.isapi(serial, "GET", "/ISAPI/VideoIntercom/callerInfo?format=json")

    async def unlock(self, serial: str, lock_id: int) -> None:
        """Open a door the way the Niko app does (ISAPI Custom unlock)."""
        reply = await self.isapi(
            serial,
            "PUT",
            "/ISAPI/Custom/VideoIntercom/unlock?format=json",
            {"lockId": lock_id},
        )
        _check_isapi_status(reply)

    async def call_operation(self, serial: str, cmd_id: int) -> None:
        """Call-signalling cmdId: 2=answer, 3=reject, 5=hang up."""
        await self._v3(
            "PUT", f"/v3/devconfig/v1/call/{serial}/operation", params={"cmdId": str(cmd_id)}
        )

    async def answer_call(self, serial: str) -> None:
        await self.call_operation(serial, 2)

    async def hangup_call(self, serial: str) -> None:
        await self.call_operation(serial, 5)

    async def unlock_v3(self, serial: str, channel: int, lock_id: int) -> None:
        """Generic Hik-Connect remote unlock; fallback if the ISAPI path fails."""
        await self._v3(
            "PUT",
            f"/v3/devconfig/v1/call/{serial}/{channel}/remote/unlock",
            params={"srcId": "1", "lockId": str(lock_id), "userType": "0"},
        )

    # ------------------------------------------------------------- transport

    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {
            "clientType": CLIENT_TYPE,
            "osVersion": OS_VERSION,
            "clientVersion": CLIENT_VERSION,
            "netType": "WIFI",
            "clientNo": "",
            "appChannel": "",
            "customno": CUSTOM_NO,
            "featureCode": self._feature_code,
            "lang": self._lang,
            "appId": APP_ID,
            "User-Agent": "okhttp/3.12.1",
        }
        if self.area_id is not None:
            headers["areaId"] = str(self.area_id)
        if auth and self.session_id:
            headers["sessionId"] = self.session_id
        return headers

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.api_url}{path}"
        try:
            async with self._http.request(
                method,
                url,
                params=params,
                data=form,
                headers=self._headers(auth),
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise NikoAccessSessionError("HTTP 401", 401)
                if resp.status >= 400:
                    raise NikoAccessError(f"HTTP {resp.status} for {path}: {text[:200]}")
        except aiohttp.ClientError as err:
            raise NikoAccessError(f"Request to {path} failed: {err}") from err
        except TimeoutError as err:
            raise NikoAccessError(f"Request to {path} timed out") from err
        try:
            data = json.loads(text)
        except ValueError as err:
            raise NikoAccessError(f"Non-JSON reply from {path}: {text[:200]}") from err
        _LOGGER.debug("%s %s -> %s", method, path, _redact(data))
        return data

    async def _v3(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        await self._ensure_session()
        for attempt in (0, 1):
            try:
                data = await self._raw(method, path, **kwargs)
            except NikoAccessSessionError:
                if attempt:
                    raise
                await self.login()
                continue
            code = _meta_code(data)
            if code == CODE_OK:
                return data
            if code in CODE_SESSION_INVALID and not attempt:
                await self.login()
                continue
            raise NikoAccessError(f"{path}: {_meta_message(data)}", code)
        raise NikoAccessError(f"{path}: retry exhausted")  # pragma: no cover

    async def _v2(self, method: str, path: str, *, form: dict[str, str]) -> dict[str, Any]:
        await self._ensure_session()
        for attempt in (0, 1):
            full_form = {
                **form,
                "sessionId": self.session_id or "",
                "clientType": CLIENT_TYPE,
                "osVersion": OS_VERSION,
                "clientVersion": CLIENT_VERSION,
                "netType": "WIFI",
                "clientNo": "",
                "appChannel": "",
            }
            if self.area_id:
                full_form["areaId"] = str(self.area_id)
            try:
                data = await self._raw(method, path, form=full_form)
            except NikoAccessSessionError:
                if attempt:
                    raise
                await self.login()
                continue
            code = _result_code(data)
            if code == 0:
                return data
            if code in CODE_SESSION_INVALID and not attempt:
                await self.login()
                continue
            raise NikoAccessError(f"{path}: {data.get('resultDes') or data}", code)
        raise NikoAccessError(f"{path}: retry exhausted")  # pragma: no cover


# ---------------------------------------------------------------- helpers


def _normalise_domain(domain: str) -> str:
    domain = domain.rstrip("/")
    return domain if domain.startswith("http") else f"https://{domain}"


def _meta_code(data: dict[str, Any]) -> int | None:
    meta = data.get("meta")
    if isinstance(meta, dict):
        try:
            return int(meta.get("code"))
        except (TypeError, ValueError):
            return None
    return None


def _meta_message(data: dict[str, Any]) -> str:
    meta = data.get("meta") or {}
    return meta.get("message") or meta.get("langMsg") or "unknown error"


def _result_code(data: dict[str, Any]) -> int | None:
    if "resultCode" not in data and "meta" in data:
        return 0 if _meta_code(data) == CODE_OK else _meta_code(data)
    try:
        return int(data.get("resultCode"))
    except (TypeError, ValueError):
        return None


def _json_or_none(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def _check_isapi_status(reply: Any) -> None:
    """ISAPI JSON replies carry statusCode 1 on success."""
    if not isinstance(reply, dict):
        return
    status = reply.get("ResponseStatus", reply)
    code = status.get("statusCode")
    if code is not None and int(code) != 1:
        raise NikoAccessError(
            f"Device refused: {status.get('subStatusCode') or status.get('statusString')}",
            code,
        )


_SENSITIVE = {"sessionId", "rfSessionId", "refreshSessionId", "password", "tempToken"}


def _redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: "***" if k in _SENSITIVE else _redact(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    return data
