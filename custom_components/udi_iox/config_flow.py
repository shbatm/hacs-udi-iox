"""Config flow for Universal Devices IoX integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse

import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.components import ssdp
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from pyisyox import (
    Controller,
    ISYConnectionError,
    ISYInvalidAuthError,
    ISYResponseParseError,
    PortalAuth,
)

from .const import (
    CONF_ENABLE_NETWORKING,
    CONF_ENABLE_PROGRAMS,
    CONF_ENABLE_VARIABLES,
    CONF_IGNORE_STRING,
    CONF_RESTORE_LIGHT_STATE,
    CONF_SENSOR_STRING,
    DEFAULT_ENABLE_NETWORKING,
    DEFAULT_ENABLE_PROGRAMS,
    DEFAULT_ENABLE_VARIABLES,
    DEFAULT_IGNORE_STRING,
    DEFAULT_RESTORE_LIGHT_STATE,
    DEFAULT_SENSOR_STRING,
    DOMAIN,
    HTTP_PORT,
    HTTPS_PORT,
    ISY_CONF_UUID,
    ISY_URL_POSTFIX,
    SCHEME_HTTP,
    SCHEME_HTTPS,
    UDN_UUID_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


def _data_schema(schema_input: dict[str, Any]) -> vol.Schema:
    """Generate user-step schema with defaults preserved across attempts."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=schema_input.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(
                CONF_VERIFY_SSL,
                default=schema_input.get(CONF_VERIFY_SSL, False),
            ): bool,
        },
        extra=vol.ALLOW_EXTRA,
    )


def _options_schema(options: Mapping[str, Any]) -> vol.Schema:
    """Shared by :meth:`ConfigFlow.async_step_options` and
    :meth:`OptionsFlowHandler.async_step_init` so the two never drift."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_IGNORE_STRING,
                default=options.get(CONF_IGNORE_STRING, DEFAULT_IGNORE_STRING),
            ): str,
            vol.Optional(
                CONF_SENSOR_STRING,
                default=options.get(CONF_SENSOR_STRING, DEFAULT_SENSOR_STRING),
            ): str,
            vol.Required(
                CONF_RESTORE_LIGHT_STATE,
                default=options.get(
                    CONF_RESTORE_LIGHT_STATE, DEFAULT_RESTORE_LIGHT_STATE
                ),
            ): bool,
            vol.Required(
                CONF_ENABLE_VARIABLES,
                default=options.get(CONF_ENABLE_VARIABLES, DEFAULT_ENABLE_VARIABLES),
            ): bool,
            vol.Required(
                CONF_ENABLE_PROGRAMS,
                default=options.get(CONF_ENABLE_PROGRAMS, DEFAULT_ENABLE_PROGRAMS),
            ): bool,
            vol.Required(
                CONF_ENABLE_NETWORKING,
                default=options.get(CONF_ENABLE_NETWORKING, DEFAULT_ENABLE_NETWORKING),
            ): bool,
        }
    )


async def validate_input(
    hass: core.HomeAssistant, data: dict[str, Any]
) -> dict[str, str]:
    """Open a short-lived connection to confirm creds and return entry metadata."""
    user = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]
    host = data[CONF_HOST]
    parsed_host = urlparse(host)
    verify_ssl = data.get(CONF_VERIFY_SSL, False)

    if parsed_host.scheme not in (SCHEME_HTTP, SCHEME_HTTPS):
        _LOGGER.error("Host value must include http:// or https://")
        raise InvalidHost

    controller = Controller(
        host,
        auth=PortalAuth(user, password),
        verify_ssl=verify_ssl,
    )

    try:
        async with asyncio.timeout(30):
            # start_websocket=False keeps the validation light — we just
            # want to confirm auth + load the config payload.
            await controller.connect(start_websocket=False)
        try:
            uuid = controller.config.uuid
            # User-assigned controller name → integration card title;
            # falls back to URL hostname.
            root_name = controller.name
        finally:
            await controller.stop()
    except ISYInvalidAuthError as error:
        raise InvalidAuth from error
    except ISYConnectionError as error:
        raise CannotConnect from error
    except ISYResponseParseError as error:
        raise CannotConnect from error

    title = root_name or parsed_host.hostname or host
    return {
        "title": title,
        ISY_CONF_UUID: uuid,
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Universal Devices IoX."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the IoX config flow."""
        self.discovered_conf: dict[str, Any] = {}
        #: Filled by :meth:`async_step_user` after credentials validate;
        #: carried into :meth:`async_step_options` so the final
        #: :meth:`async_create_entry` call has both ``data`` and ``options``.
        self._user_input: dict[str, Any] | None = None
        self._entry_title: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        info: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidHost:
                errors["base"] = "invalid_host"
            except InvalidAuth:
                errors[CONF_PASSWORD] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                await self.async_set_unique_id(
                    info[ISY_CONF_UUID], raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                # Carry the validated credentials into the options step
                # rather than creating the entry now; this lands the user
                # on the same configurable surface (sensor strings,
                # variables/programs/network toggles, light restore) the
                # post-install options flow exposes, with sensible
                # defaults preselected — Bronze IQS rule against
                # creating an entry the user can't tune in the same
                # flow.
                self._user_input = user_input
                self._entry_title = info["title"]
                return await self.async_step_options()

        return self.async_show_form(
            step_id="user",
            data_schema=_data_schema(self.discovered_conf),
            errors=errors,
            description_placeholders={
                "sample_ip": "https://eisy.local:443",
            },
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm/adjust options before create. Same schema as
        ``OptionsFlowHandler.async_step_init``; bounces back to user
        if credentials weren't captured first."""
        if self._user_input is None:
            return await self.async_step_user()
        if user_input is not None:
            return self.async_create_entry(
                title=self._entry_title or "",
                data=self._user_input,
                options=user_input,
            )

        return self.async_show_form(
            step_id="options",
            data_schema=_options_schema({}),
        )

    async def _async_set_unique_id_or_update(
        self, isy_mac: str, ip_address: str, port: int | None
    ) -> None:
        """Abort and update the host on change."""
        existing_entry = await self.async_set_unique_id(isy_mac)
        if not existing_entry:
            return
        if existing_entry.source == config_entries.SOURCE_IGNORE:
            raise AbortFlow("already_configured")
        parsed_url = urlparse(existing_entry.data[CONF_HOST])
        if parsed_url.hostname != ip_address:
            new_netloc = ip_address
            if port:
                new_netloc = f"{ip_address}:{port}"
            elif parsed_url.port:
                new_netloc = f"{ip_address}:{parsed_url.port}"
            self.hass.config_entries.async_update_entry(
                existing_entry,
                data={
                    **existing_entry.data,
                    CONF_HOST: urlunparse(
                        (
                            parsed_url.scheme,
                            new_netloc,
                            parsed_url.path,
                            parsed_url.query,
                            parsed_url.fragment,
                            None,
                        )
                    ),
                },
            )
        raise AbortFlow("already_configured")

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> config_entries.ConfigFlowResult:
        """Handle a discovered IoX device via DHCP."""
        friendly_name = discovery_info.hostname
        # eisy / Polisy serve the IoX API over HTTPS on :443.
        url = f"https://{discovery_info.ip}:{HTTPS_PORT}"
        mac = discovery_info.macaddress
        isy_mac = (
            f"{mac[0:2]}:{mac[2:4]}:{mac[4:6]}:{mac[6:8]}:{mac[8:10]}:{mac[10:12]}"
        )
        await self._async_set_unique_id_or_update(isy_mac, discovery_info.ip, None)

        self.discovered_conf = {
            CONF_NAME: friendly_name,
            CONF_HOST: url,
        }

        self.context["title_placeholders"] = self.discovered_conf
        return await self.async_step_user()

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> config_entries.ConfigFlowResult:
        """Handle a discovered IoX device via SSDP."""
        # Belt-and-suspenders: the manifest matcher already pins
        # ``deviceType=X_IoX_Device:1`` (IoX-6+ only — pre-6 firmware
        # advertises ``X_Insteon_Lighting_Device:1``), but a future
        # IoX-5.x SKU could in theory reuse the deviceType. Reject any
        # ``modelVersion`` that isn't 6.x so we never offer this
        # integration for a controller pyisyox doesn't support.
        model_version = discovery_info.upnp.get("modelVersion", "")
        if not model_version.startswith("6."):
            return self.async_abort(reason="unsupported_firmware")

        friendly_name = discovery_info.upnp[ssdp.ATTR_UPNP_FRIENDLY_NAME]
        url = discovery_info.ssdp_location
        assert isinstance(url, str)
        parsed_url = urlparse(url)
        mac = discovery_info.upnp[ssdp.ATTR_UPNP_UDN]
        mac = mac.removeprefix(UDN_UUID_PREFIX)
        url = url.removesuffix(ISY_URL_POSTFIX)

        port = HTTP_PORT
        if parsed_url.port:
            port = parsed_url.port
        elif parsed_url.scheme == SCHEME_HTTPS:
            port = HTTPS_PORT

        assert isinstance(parsed_url.hostname, str)
        await self._async_set_unique_id_or_update(mac, parsed_url.hostname, port)

        self.discovered_conf = {
            CONF_NAME: friendly_name,
            CONF_HOST: url,
        }

        self.context["title_placeholders"] = self.discovered_conf
        return await self.async_step_user()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle reauth."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle reauth input."""
        errors = {}
        existing_entry = self._get_reauth_entry()
        existing_data = existing_entry.data
        if user_input is not None:
            new_data = {
                **existing_data,
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                await validate_input(self.hass, new_data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors[CONF_PASSWORD] = "invalid_auth"
            else:
                cfg_entries = self.hass.config_entries
                cfg_entries.async_update_entry(existing_entry, data=new_data)
                await cfg_entries.async_reload(existing_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        self.context["title_placeholders"] = {
            CONF_NAME: existing_entry.title,
            CONF_HOST: existing_data[CONF_HOST],
        }
        return self.async_show_form(
            description_placeholders={
                CONF_HOST: existing_data[CONF_HOST],
                "sample_ip": "https://eisy.local:443",
            },
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=existing_data[CONF_USERNAME]
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an option flow for IoX."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self.config_entry.options),
        )


class InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate the host value is invalid."""


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""
