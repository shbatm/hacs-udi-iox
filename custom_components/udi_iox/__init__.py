"""Support the Universal Devices IoX (eisy) controllers."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import homeassistant.helpers.device_registry as dr
import probatio
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
    PortalAuth,
)

from .const import (
    _LOGGER,
    CONF_ENABLE_NETWORKING,
    CONF_ENABLE_PROGRAMS,
    CONF_ENABLE_VARIABLES,
    CONF_IGNORE_STRING,
    CONF_NETWORK,
    CONF_SENSOR_STRING,
    DEFAULT_IGNORE_STRING,
    DEFAULT_PROGRAM_STRING,
    DEFAULT_SENSOR_STRING,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
)
from .controller_events import IsyControllerEvents
from .helpers import (
    _categorize_nodes,
    _categorize_program_devices,
    _categorize_programs,
    _categorize_variables,
)
from .models import IsyConfigEntry, IsyData
from .program_device import program_device_info
from .services import async_setup_services
from .util import _async_cleanup_registry_entries

CONFIG_SCHEMA = probatio.Schema(
    cv.deprecated(DOMAIN),
    extra=probatio.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the IoX integration."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IsyConfigEntry) -> None:
    """Set up an IoX config entry."""
    isy_data = IsyData()
    entry.runtime_data = isy_data

    isy_config = entry.data
    isy_options = entry.options

    user = isy_config[CONF_USERNAME]
    password = isy_config[CONF_PASSWORD]
    host = isy_config[CONF_HOST]
    verify_ssl = isy_config.get(CONF_VERIFY_SSL, False)

    enable_variables = isy_options.get(CONF_ENABLE_VARIABLES, True)
    enable_programs = isy_options.get(CONF_ENABLE_PROGRAMS, True)
    enable_networking = isy_options.get(CONF_ENABLE_NETWORKING, False)
    ignore_identifier = isy_options.get(CONF_IGNORE_STRING, DEFAULT_IGNORE_STRING)
    isy_data.sensor_string = isy_options.get(CONF_SENSOR_STRING, DEFAULT_SENSOR_STRING)

    controller = Controller(
        host,
        auth=PortalAuth(user, password),
        verify_ssl=verify_ssl,
    )

    try:
        async with asyncio.timeout(60):
            await controller.connect()
    except TimeoutError as err:
        raise ConfigEntryNotReady(
            f"Timed out connecting to the IoX controller; trying again later: {err}"
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
        _categorize_programs(
            isy_data, controller.programs, ignore_identifier=ignore_identifier
        )
        _categorize_program_devices(
            isy_data,
            controller.programs,
            program_prefix=DEFAULT_PROGRAM_STRING,
            ignore_identifier=ignore_identifier,
        )
        for program in isy_data.program_devices:
            isy_data.devices[f"program_{program.address}"] = program_device_info(
                controller, program, host
            )

    if enable_variables and controller.variables:
        _categorize_variables(isy_data, controller.variables)
        isy_data.devices[CONF_VARIABLES] = _create_service_device_info(
            controller, host, name=CONF_VARIABLES.title(), unique_id=CONF_VARIABLES
        )

    if enable_networking and controller.network_resources:
        isy_data.net_resources = list(controller.network_resources.values())
        isy_data.devices[CONF_NETWORK] = _create_service_device_info(
            controller, host, name="Network Resources", unique_id=CONF_NETWORK
        )

    hub = _async_get_or_create_isy_device_in_registry(hass, entry, controller, host)
    _async_link_parent_devices(hass, entry, isy_data, hub.id)

    # Platform setup runs each entity's async_added_to_hass synchronously
    # via async_forward_entry_setups, and entities subscribe through
    # isy_data.controller_events — so the registry must exist first.
    isy_data.controller_events = IsyControllerEvents(hass, isy_data, entry.entry_id)
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


def _controller_label(controller: Controller, host: str) -> str:
    """Controller name → URL hostname → host. Used by the hub +
    Variables + Network service devices for a common prefix."""
    return controller.name or urlparse(host).hostname or host


@callback
def _async_link_parent_devices(
    hass: HomeAssistant, entry: IsyConfigEntry, isy_data: IsyData, hub_id: str
) -> None:
    """Set ``via_device_id`` on every device's :class:`DeviceInfo`.

    ``via_device_id`` takes a registry id, so a parent device is
    registered here before the child's entities create the child.
    A parent missing from ``isy_data.devices`` (e.g. an ignored
    controller node) falls back to the hub.
    """
    registry = dr.async_get(hass)
    for key, info in isy_data.devices.items():
        parent_info = isy_data.devices.get(isy_data.device_parents.get(key, ""))
        if parent_info is None:
            info.via_device_id = hub_id
            continue
        parent_info.via_device_id = hub_id
        parent = registry.async_get_or_create_from_info(
            config_entry_id=entry.entry_id, device_info=parent_info
        )
        info.via_device_id = parent.id


@callback
def _async_get_or_create_isy_device_in_registry(
    hass: HomeAssistant, entry: IsyConfigEntry, controller: Controller, host: str
) -> dr.DeviceEntry:
    device_registry = dr.async_get(hass)
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, controller.config.uuid)},
        manufacturer=MANUFACTURER,
        name=_controller_label(controller, host),
        sw_version=controller.config.version,
        configuration_url=host,
    )


def _create_service_device_info(
    controller: Controller, host: str, name: str, unique_id: str
) -> DeviceInfo:
    """Create device info for IoX service devices (Variables, Network)."""
    return DeviceInfo(
        identifiers={
            (
                DOMAIN,
                f"{controller.config.uuid}_{unique_id}",
            )
        },
        manufacturer=MANUFACTURER,
        name=f"{_controller_label(controller, host)} {name}",
        sw_version=controller.config.version,
        configuration_url=host,
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_unload_entry(hass: HomeAssistant, entry: IsyConfigEntry) -> None:
    """Unload a config entry."""
    try:
        await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    finally:
        controller: Controller = entry.runtime_data.root
        _LOGGER.debug("IoX stopping event stream and automatic updates")
        await controller.stop()


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: IsyConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Remove an IoX config entry from a device."""
    return not device_entry.identifiers.intersection(
        (DOMAIN, unique_id) for unique_id in config_entry.runtime_data.devices
    )
