"""IoX services.

* ``send_node_command`` — friendly-named entity command, dispatched
  through :meth:`ISYNodeEntity.async_send_node_command`.
* ``set_variable`` — writes through
  :meth:`pyisyox.Controller.set_variable_value` /
  :meth:`set_variable_init`.
* ``system_query`` — calls :meth:`pyisyox.Controller.refresh`.
* ``send_program_command`` / ``run_network_resource`` — registered for
  schema continuity but raise on call: pyisyox doesn't expose
  controller-level program commands or typed network resources yet.
"""

from __future__ import annotations

from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COMMAND,
    CONF_NAME,
    CONF_TYPE,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.service import entity_service_call
from pyisyox import ProgramCommand
from pyisyox.constants import COMMAND_FRIENDLY_NAME

from .const import DOMAIN

# Domain-wide services
SERVICE_SYSTEM_QUERY = "system_query"
SERVICE_SET_VARIABLE = "set_variable"
SERVICE_SEND_PROGRAM_COMMAND = "send_program_command"
SERVICE_RUN_NETWORK_RESOURCE = "run_network_resource"

INTEGRATION_SERVICES = [
    SERVICE_SYSTEM_QUERY,
    SERVICE_SET_VARIABLE,
    SERVICE_SEND_PROGRAM_COMMAND,
    SERVICE_RUN_NETWORK_RESOURCE,
]

# Entity-targeting services (light, switch, climate, fan, cover, lock, etc.)
SERVICE_SEND_NODE_COMMAND = "send_node_command"
SERVICE_RENAME_NODE = "rename_node"

CONF_VALUE = "value"
CONF_INIT = "init"
CONF_ISY = "isy"

VALID_NODE_COMMANDS = [
    "beep",
    "brighten",
    "dim",
    "disable",
    "enable",
    "fade_down",
    "fade_stop",
    "fade_up",
    "fast_off",
    "fast_on",
    "query",
]
#: Service verbs are 1:1 with the snake-case method names on
#: ``pyisyox.Program``. Derived from the upstream ``ProgramCommand``
#: StrEnum so new pyisyox verbs surface here automatically without
#: having to maintain a parallel list. Dispatch happens via
#: ``getattr(program, command)`` — the snake-case → camelCase wire
#: mapping lives entirely in pyisyox.
VALID_PROGRAM_COMMANDS = [member.name.lower() for member in ProgramCommand]
#: Folders only support the subset shared by ``_ProgramBase``;
#: the rest raise AttributeError on a folder.
FOLDER_COMMANDS = frozenset({"run", "stop", "enable", "disable"})


def _valid_iox_command(value: Any) -> str:
    """Validate the command id is one pyisyox knows about."""
    cmd = str(value).upper()
    if cmd in COMMAND_FRIENDLY_NAME:
        return cmd
    raise vol.Invalid(f"Unknown IoX command: {value!r}")


SCHEMA_GROUP = "name-address"

SERVICE_SYSTEM_QUERY_SCHEMA = vol.Schema(
    {vol.Optional(CONF_ADDRESS): cv.string, vol.Optional(CONF_ISY): cv.string}
)

SERVICE_SEND_NODE_COMMAND_SCHEMA = {
    vol.Required(CONF_COMMAND): vol.In(VALID_NODE_COMMANDS)
}

SERVICE_RENAME_NODE_SCHEMA = {vol.Required(CONF_NAME): cv.string}

SERVICE_SET_VARIABLE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): vol.Coerce(int),
        vol.Required(CONF_TYPE): vol.All(vol.Coerce(int), vol.Range(1, 2)),
        vol.Required(CONF_VALUE): vol.Coerce(int),
        vol.Optional(CONF_INIT, default=False): bool,
        vol.Optional(CONF_ISY): cv.string,
    }
)

SERVICE_SEND_PROGRAM_COMMAND_SCHEMA = vol.All(
    cv.has_at_least_one_key(CONF_ADDRESS, CONF_NAME),
    vol.Schema(
        {
            vol.Exclusive(CONF_NAME, SCHEMA_GROUP): cv.string,
            vol.Exclusive(CONF_ADDRESS, SCHEMA_GROUP): cv.string,
            vol.Required(CONF_COMMAND): vol.In(VALID_PROGRAM_COMMANDS),
            vol.Optional(CONF_ISY): cv.string,
        }
    ),
)

SERVICE_RUN_NETWORK_RESOURCE_SCHEMA = vol.All(
    cv.has_at_least_one_key(CONF_ADDRESS, CONF_NAME),
    vol.Schema(
        {
            vol.Exclusive(CONF_NAME, SCHEMA_GROUP): cv.string,
            vol.Exclusive(CONF_ADDRESS, SCHEMA_GROUP): vol.Coerce(int),
            vol.Optional(CONF_ISY): cv.string,
        }
    ),
)


def _select_isy_data(hass: HomeAssistant, isy_name: str | None):
    """Yield (entry, isy_data) tuples for the targeted controller(s)."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        isy_data = entry.runtime_data
        if isy_data is None:
            continue
        # The legacy isy_name knob targeted by ISY display name; in v6
        # ControllerConfig has no name field, so the user passes the
        # uuid (preferred) or we accept any value to mean "first
        # match" if there's only one entry.
        if isy_name and isy_name != isy_data.uuid:
            continue
        yield entry, isy_data


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain-wide IoX services."""
    existing_services = hass.services.async_services().get(DOMAIN)
    if existing_services and any(
        service in INTEGRATION_SERVICES for service in existing_services
    ):
        # Already registered for an earlier entry; the services live for
        # the lifetime of the integration.
        return

    async def async_system_query(call: ServiceCall) -> None:
        """Refresh the controller's node + property cache."""
        isy_name = call.data.get(CONF_ISY)
        targeted = list(_select_isy_data(hass, isy_name))
        if not targeted:
            raise HomeAssistantError(
                f"No IoX controller matched isy={isy_name!r}"
            )
        for _, isy_data in targeted:
            await isy_data.root.refresh()

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SYSTEM_QUERY,
        service_func=async_system_query,
        schema=SERVICE_SYSTEM_QUERY_SCHEMA,
    )

    async def async_set_variable(call: ServiceCall) -> None:
        """Write a variable via the controller."""
        var_id = call.data[CONF_ADDRESS]
        var_type = call.data[CONF_TYPE]
        value = call.data[CONF_VALUE]
        init = call.data[CONF_INIT]
        isy_name = call.data.get(CONF_ISY)

        targeted = list(_select_isy_data(hass, isy_name))
        if not targeted:
            raise HomeAssistantError(
                f"No IoX controller matched isy={isy_name!r}"
            )
        for _, isy_data in targeted:
            controller = isy_data.root
            if init:
                await controller.set_variable_init(var_type, var_id, value)
            else:
                await controller.set_variable_value(var_type, var_id, value)

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SET_VARIABLE,
        service_func=async_set_variable,
        schema=SERVICE_SET_VARIABLE_SCHEMA,
    )

    async def async_send_program_command(call: ServiceCall) -> None:
        """Send a verb (``run`` / ``run_then`` / ``enable`` / …) to a
        program or folder by id or name.

        Resolution order: id wins over name when both are supplied;
        name lookup is exact-match against ``Program.name`` (programs
        preferred over folders). Folder targets are restricted to the
        subset of verbs ``ProgramFolder`` exposes — the others raise
        a targeted ``HomeAssistantError`` rather than an opaque
        ``AttributeError``.
        """
        address = call.data.get(CONF_ADDRESS)
        name = call.data.get(CONF_NAME)
        command = call.data[CONF_COMMAND]
        isy_name = call.data.get(CONF_ISY)

        targeted = list(_select_isy_data(hass, isy_name))
        if not targeted:
            raise HomeAssistantError(
                f"No IoX controller matched isy={isy_name!r}"
            )

        for _, isy_data in targeted:
            controller = isy_data.root
            programs = controller.programs
            folders = controller.program_folders

            target = None
            if address is not None:
                program_id = str(address)
                target = programs.get(program_id) or folders.get(program_id)
                if target is None:
                    raise HomeAssistantError(
                        f"No program or folder with id {address!r} on this controller"
                    )
            else:
                target = next(
                    (p for p in programs.values() if p.name == name),
                    None,
                ) or next(
                    (f for f in folders.values() if f.name == name),
                    None,
                )
                if target is None:
                    raise HomeAssistantError(
                        f"No program or folder named {name!r} on this controller"
                    )

            # Folders only support the subset shared by _ProgramBase.
            if target.address in folders and command not in FOLDER_COMMANDS:
                raise HomeAssistantError(
                    f"Folder {target.address} does not support command {command!r}"
                )

            await getattr(target, command)()

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SEND_PROGRAM_COMMAND,
        service_func=async_send_program_command,
        schema=SERVICE_SEND_PROGRAM_COMMAND_SCHEMA,
    )

    async def async_run_network_resource(call: ServiceCall) -> None:
        """Fire a configured IoX network resource by id or name."""
        address = call.data.get(CONF_ADDRESS)
        name = call.data.get(CONF_NAME)
        isy_name = call.data.get(CONF_ISY)

        targeted = list(_select_isy_data(hass, isy_name))
        if not targeted:
            raise HomeAssistantError(
                f"No IoX controller matched isy={isy_name!r}"
            )

        # The schema enforces "at least one of name/address" — if
        # address is given, target it directly (cheaper than resolving
        # by name and works for callers carrying the resource id).
        # When both are given, address wins (matches the legacy
        # send_program_command resolution order).
        for _, isy_data in targeted:
            controller = isy_data.root
            resources = controller.network_resources
            if address is not None:
                resource_id = str(address)
                if resource_id not in resources:
                    raise HomeAssistantError(
                        f"No network resource with id {address!r} on this controller"
                    )
                await controller.run_network_resource(resource_id)
                continue
            # Resolve by name — first match wins.
            match = next(
                (r for r in resources.values() if r.name == name), None
            )
            if match is None:
                raise HomeAssistantError(
                    f"No network resource named {name!r} on this controller"
                )
            await match.run()

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_RUN_NETWORK_RESOURCE,
        service_func=async_run_network_resource,
        schema=SERVICE_RUN_NETWORK_RESOURCE_SCHEMA,
    )

    async def _async_send_node_command(call: ServiceCall) -> None:
        await entity_service_call(
            hass, async_get_platforms(hass, DOMAIN), "async_send_node_command", call
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SEND_NODE_COMMAND,
        schema=cv.make_entity_service_schema(SERVICE_SEND_NODE_COMMAND_SCHEMA),
        service_func=_async_send_node_command,
    )

    async def _async_rename_node(call: ServiceCall) -> None:
        await entity_service_call(
            hass, async_get_platforms(hass, DOMAIN), "async_rename_node", call
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_RENAME_NODE,
        schema=cv.make_entity_service_schema(SERVICE_RENAME_NODE_SCHEMA),
        service_func=_async_rename_node,
    )


# Compat shim — lock.py still imports this. The function is a no-op
# while Z-Wave user-code services are unsupported; lock.py keeps the
# call so the platform's setup_entry doesn't change shape later.
@callback
def async_setup_lock_services(hass: HomeAssistant) -> None:
    """No-op placeholder for Z-Wave lock services."""
