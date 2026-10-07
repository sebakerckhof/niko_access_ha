"""Config flow for Niko Access."""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    NikoAccessAuthError,
    NikoAccessClient,
    NikoAccessError,
    NikoAccessVerificationRequired,
    hash_password,
)
from .button import parse_lock_ids
from .const import (
    CAMERA_QUALITIES,
    CONF_API_URL,
    CONF_CAMERA_CHANNEL,
    CONF_CAMERA_IP,
    CONF_CAMERA_QUALITY,
    CONF_FEATURE_CODE,
    CONF_LOCK_IDS,
    CONF_PASSWORD_MD5,
    CONF_SCAN_INTERVAL,
    DEFAULT_CAMERA_CHANNEL,
    DEFAULT_CAMERA_QUALITY,
    DEFAULT_LOCK_IDS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .discovery import async_find_ip

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


class NikoAccessConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _try_login(
        self, username: str, password: str, feature_code: str
    ) -> tuple[NikoAccessClient | None, dict[str, str]]:
        client = NikoAccessClient(
            async_get_clientsession(self.hass), username, hash_password(password), feature_code
        )
        try:
            await client.login()
        except NikoAccessAuthError:
            return None, {"base": "invalid_auth"}
        except NikoAccessVerificationRequired:
            return None, {"base": "verification_required"}
        except NikoAccessError:
            _LOGGER.exception("Niko Access login failed")
            return None, {"base": "cannot_connect"}
        return client, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            feature_code = uuid.uuid4().hex
            client, errors = await self._try_login(
                username, user_input[CONF_PASSWORD], feature_code
            )
            if client:
                await self.async_set_unique_id(client.user_id or username.lower())
                self._abort_if_unique_id_configured()
                # Auto-detect the door-station IP now (SADP) so the camera is
                # available immediately, without the user opening Configure.
                options: dict[str, Any] = {}
                camera_ip = await self._async_detect_camera_ip(client)
                if camera_ip:
                    options = {
                        CONF_CAMERA_IP: camera_ip,
                        CONF_CAMERA_CHANNEL: DEFAULT_CAMERA_CHANNEL,
                        CONF_CAMERA_QUALITY: DEFAULT_CAMERA_QUALITY,
                    }
                return self.async_create_entry(
                    title=username,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD_MD5: hash_password(user_input[CONF_PASSWORD]),
                        CONF_FEATURE_CODE: feature_code,
                        CONF_API_URL: client.api_url,
                    },
                    options=options,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CREDENTIALS_SCHEMA, user_input),
            errors=errors,
        )

    async def _async_detect_camera_ip(self, client: NikoAccessClient) -> str | None:
        """Best-effort SADP lookup of the door station's LAN IP during setup."""
        try:
            devices = await client.get_devices()
        except NikoAccessError:
            return None
        for device in devices:
            with contextlib.suppress(Exception):
                ip = await async_find_ip(device.serial)
                if ip:
                    return ip
        return None

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client, errors = await self._try_login(
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry.data[CONF_FEATURE_CODE],
            )
            if client:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD_MD5: hash_password(user_input[CONF_PASSWORD]),
                        CONF_API_URL: client.api_url,
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> NikoAccessOptionsFlow:
        return NikoAccessOptionsFlow()


class NikoAccessOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            lock_ids = user_input.get(CONF_LOCK_IDS, "").strip()
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()
            if lock_ids and not parse_lock_ids(lock_ids):
                errors[CONF_LOCK_IDS] = "invalid_lock_ids"
            if camera_ip and not _valid_ip(camera_ip):
                errors[CONF_CAMERA_IP] = "invalid_ip"
            if not errors:
                return self.async_create_entry(
                    data={**user_input, CONF_LOCK_IDS: lock_ids, CONF_CAMERA_IP: camera_ip}
                )

        # Auto-detect the door-station IP (SADP) when none is configured yet, so
        # the field is pre-filled instead of the user hunting for the address.
        suggested = dict(user_input or self.config_entry.options)
        if not suggested.get(CONF_CAMERA_IP):
            found = await self._async_discover_ip()
            if found:
                suggested[CONF_CAMERA_IP] = found

        schema = vol.Schema(
            {
                vol.Optional(CONF_LOCK_IDS, default=DEFAULT_LOCK_IDS): str,
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=60)
                ),
                vol.Optional(CONF_CAMERA_IP, default=""): str,
                vol.Required(
                    CONF_CAMERA_CHANNEL, default=DEFAULT_CAMERA_CHANNEL
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
                vol.Required(CONF_CAMERA_QUALITY, default=DEFAULT_CAMERA_QUALITY): vol.In(
                    CAMERA_QUALITIES
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def _async_discover_ip(self) -> str | None:
        """Best-effort SADP lookup of the door station's LAN IP by serial."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        serials = list(coordinator.data) if coordinator and coordinator.data else []
        for serial in serials:
            with contextlib.suppress(Exception):
                ip = await async_find_ip(serial)
                if ip:
                    return ip
        return None


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
