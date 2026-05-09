"""Support the Universal Devices IoX (eisy / Polisy) controllers."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import homeassistant.helpers.device_registry as dr
import voluptuous as vol
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VARIABLES,
    CONF_VERIFY_SSL,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from pyisyox import (
    Controller,
    ISYConnectionError,
    ISYInvalidAuthError,
    ISYResponseParseError,
    LocalAuth,
    PortalAuth,
)

from .const import (
    _LOGGER,
    AUTH_MODE_LOCAL,
    CONF_AUTH_MODE,
    CONF_ENABLE_NETWORKING,
    CONF_ENABLE_PROGRAMS,
    CONF_ENABLE_VARIABLES,
    CONF_TLS_VER,
    DEFAULT_AUTH_MODE,
    DEFAULT_TLS_VERSION,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
)
from .controller_events import IsyControllerEvents
from .helpers import _categorize_nodes, _categorize_programs, _categorize_variables
from .models import IsyConfigEntry, IsyData
from .services import async_setup_services
from .util import _async_cleanup_registry_entries

CONFIG_SCHEMA = vol.Schema(
    cv.deprecated(DOMAIN),
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the IoX integration."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IsyConfigEntry) -> bool:
    """Set up an IoX config entry."""
    isy_data = IsyData()
    entry.runtime_data = isy_data

    isy_config = entry.data
    isy_options = entry.options

    user = isy_config[CONF_USERNAME]
    password = isy_config[CONF_PASSWORD]
    host = isy_config[CONF_HOST]
    auth_mode = isy_config.get(CONF_AUTH_MODE, DEFAULT_AUTH_MODE)
    tls_version_value = isy_config.get(CONF_TLS_VER, DEFAULT_TLS_VERSION)
    tls_version = (
        tls_version_value if tls_version_value != DEFAULT_TLS_VERSION else None
    )
    verify_ssl = isy_config.get(CONF_VERIFY_SSL, False)

    enable_variables = isy_options.get(CONF_ENABLE_VARIABLES, True)
    enable_programs = isy_options.get(CONF_ENABLE_PROGRAMS, True)
    enable_networking = isy_options.get(CONF_ENABLE_NETWORKING, False)

    auth = (
        LocalAuth(user, password)
        if auth_mode == AUTH_MODE_LOCAL
        else PortalAuth(user, password)
    )
    controller = Controller(
        host,
        auth=auth,
        tls_version=tls_version,
        verify_ssl=verify_ssl,
    )

    try:
        async with asyncio.timeout(60):
            await controller.connect()
    except asyncio.TimeoutError as err:
        raise ConfigEntryNotReady(
            "Timed out connecting to the IoX controller; trying again later:"
            f" {err}"
        ) from err
    except ISYInvalidAuthError as err:
        raise ConfigEntryAuthFailed(
            f"Invalid credentials for the IoX controller: {err}"
        ) from err
    except ISYConnectionError as err:
        raise ConfigEntryNotReady(
            f"Failed to connect to the IoX controller: {err}"
        ) from err
    except ISYResponseParseError as err:
        raise ConfigEntryNotReady(
            "Invalid response from the IoX controller; ensure the firmware is up"
            f" to date: {err}"
        ) from err

    isy_data.root = controller

    _categorize_nodes(
        isy_data, controller.nodes, isy_options, controller=controller, host=host
    )

    if enable_programs and controller.programs:
        _categorize_programs(isy_data, controller.programs)

    if enable_variables and controller.variables:
        _categorize_variables(isy_data, controller.variables)
        isy_data.devices[CONF_VARIABLES] = _create_service_device_info(
            controller, host, name=CONF_VARIABLES.title(), unique_id=CONF_VARIABLES
        )

    if enable_networking:
        _LOGGER.debug(
            "Network resources requested but no typed wrapper is available;"
            " skipping"
        )

    _async_get_or_create_isy_device_in_registry(hass, entry, controller, host)

    # Platform setup runs each entity's async_added_to_hass synchronously
    # via async_forward_entry_setups, and entities subscribe through
    # isy_data.controller_events — so the registry must exist first.
    isy_data.controller_events = IsyControllerEvents(hass, isy_data)
    entry.async_on_unload(isy_data.controller_events.stop)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_cleanup_registry_entries(hass, entry)

    @callback
    def _async_stop(event: Event) -> None:
        """Tear down the controller on HA shutdown."""
        _LOGGER.debug("IoX stopping event stream")
        hass.async_create_task(controller.stop())

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    return True


@callback
def _async_get_or_create_isy_device_in_registry(
    hass: HomeAssistant, entry: IsyConfigEntry, controller: Controller, host: str
) -> None:
    device_registry = dr.async_get(hass)
    title_host = urlparse(host).hostname or host
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, controller.config.uuid)},
        manufacturer=MANUFACTURER,
        name=title_host,
        sw_version=controller.config.version,
        configuration_url=host,
    )


def _create_service_device_info(
    controller: Controller, host: str, name: str, unique_id: str
) -> DeviceInfo:
    """Create device info for IoX service devices (Variables, Network)."""
    title_host = urlparse(host).hostname or host
    return DeviceInfo(
        identifiers={
            (
                DOMAIN,
                f"{controller.config.uuid}_{unique_id}",
            )
        },
        manufacturer=MANUFACTURER,
        name=f"{title_host} {name}",
        sw_version=controller.config.version,
        configuration_url=host,
        via_device=(DOMAIN, controller.config.uuid),
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_unload_entry(hass: HomeAssistant, entry: IsyConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    controller: Controller = entry.runtime_data.root

    _LOGGER.debug("IoX stopping event stream and automatic updates")
    await controller.stop()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: IsyConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Remove an IoX config entry from a device."""
    return not device_entry.identifiers.intersection(
        (DOMAIN, unique_id) for unique_id in config_entry.runtime_data.devices
    )
