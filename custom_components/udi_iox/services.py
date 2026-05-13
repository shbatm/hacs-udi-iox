"""IoX services.

* ``send_node_command`` — friendly-named entity command, dispatched
  through :meth:`ISYNodeEntity.async_send_node_command`. The command id
  is validated against the node's nodedef accept set before it's sent.
* ``get_node_commands`` — response-only entity service returning the
  node's accepted-command vocabulary (wire ids + friendly names), read
  just-in-time from the nodedef.
* ``send_program_command`` — runs a verb (run, run_then, run_else,
  stop, enable, disable, …) against an IoX program or folder.

Per-network-resource fire-triggers are handled by the button platform
(``button.<resource_name>``), not a domain-level service.
"""

from __future__ import annotations

from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COMMAND,
    CONF_NAME,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.service import entity_service_call
from pyisyox import ProgramCommand
from pyisyox.constants import COMMAND_FRIENDLY_NAME

from .const import DOMAIN

# Domain-wide services
SERVICE_SEND_PROGRAM_COMMAND = "send_program_command"

INTEGRATION_SERVICES = [
    SERVICE_SEND_PROGRAM_COMMAND,
]

# Entity-targeting services (light, switch, climate, fan, cover, lock, etc.)
SERVICE_SEND_NODE_COMMAND = "send_node_command"
SERVICE_GET_NODE_COMMANDS = "get_node_commands"
SERVICE_RENAME_NODE = "rename_node"
SERVICE_SET_ZWAVE_PARAMETER = "set_zwave_parameter"
SERVICE_GET_ZWAVE_PARAMETER = "get_zwave_parameter"

CONF_VALUE = "value"
CONF_ISY = "isy"
CONF_PARAMETER = "parameter"
CONF_SIZE = "size"

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

SERVICE_SEND_NODE_COMMAND_SCHEMA = {
    vol.Required(CONF_COMMAND): vol.In(VALID_NODE_COMMANDS)
}

SERVICE_RENAME_NODE_SCHEMA = {vol.Required(CONF_NAME): cv.string}

SERVICE_SET_ZWAVE_PARAMETER_SCHEMA = {
    vol.Required(CONF_PARAMETER): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Required(CONF_VALUE): vol.Coerce(int),
    # ``select`` selectors hand the value back as a string ("1" / "2" /
    # "4"); coerce before the membership check so the schema accepts
    # both the UI's string form and an int from YAML / scripts.
    vol.Required(CONF_SIZE): vol.All(vol.Coerce(int), vol.In((1, 2, 4))),
}

SERVICE_GET_ZWAVE_PARAMETER_SCHEMA = {
    vol.Required(CONF_PARAMETER): vol.All(vol.Coerce(int), vol.Range(min=1)),
}

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


def async_get_entities(
    hass: HomeAssistant, supports: str | None = None
) -> dict[str, Entity]:
    """Collect every udi_iox entity across all platforms, keyed by entity_id.

    ``entity_service_call`` wants a ``dict[str, Entity]`` (or a callable
    returning one); handing it the raw platform list it used to accept
    now raises ``AttributeError: 'list' object has no attribute 'get'``.

    ``supports`` narrows the result to entities exposing a given method
    — node-targeted services (``send_node_command``, ``get_node_commands``,
    ``set_zwave_parameter``, etc.) only live on the ``ISYNodeEntity``
    branch, so a user targeting a scene/group entity should get a
    "no entities matched" failure from the service-call layer rather
    than an opaque ``AttributeError`` inside ``_handle_entity_call``.
    """
    entities: dict[str, Entity] = {}
    for platform in async_get_platforms(hass, DOMAIN):
        entities.update(platform.entities)
    if supports is None:
        return entities
    return {
        entity_id: entity
        for entity_id, entity in entities.items()
        if hasattr(entity, supports)
    }


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
            raise HomeAssistantError(f"No IoX controller matched isy={isy_name!r}")

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

    async def _async_send_node_command(call: ServiceCall) -> None:
        await entity_service_call(
            hass,
            async_get_entities(hass, supports="async_send_node_command"),
            "async_send_node_command",
            call,
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SEND_NODE_COMMAND,
        schema=cv.make_entity_service_schema(SERVICE_SEND_NODE_COMMAND_SCHEMA),
        service_func=_async_send_node_command,
    )

    async def _async_get_node_commands(call: ServiceCall) -> ServiceResponse:
        return await entity_service_call(
            hass,
            async_get_entities(hass, supports="async_get_node_commands"),
            "async_get_node_commands",
            call,
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_GET_NODE_COMMANDS,
        schema=cv.make_entity_service_schema({}),
        service_func=_async_get_node_commands,
        supports_response=SupportsResponse.ONLY,
    )

    async def _async_rename_node(call: ServiceCall) -> None:
        await entity_service_call(
            hass,
            async_get_entities(hass, supports="async_rename_node"),
            "async_rename_node",
            call,
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_RENAME_NODE,
        schema=cv.make_entity_service_schema(SERVICE_RENAME_NODE_SCHEMA),
        service_func=_async_rename_node,
    )

    async def _async_set_zwave_parameter(call: ServiceCall) -> None:
        await entity_service_call(
            hass,
            async_get_entities(hass, supports="async_set_zwave_parameter"),
            "async_set_zwave_parameter",
            call,
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SET_ZWAVE_PARAMETER,
        schema=cv.make_entity_service_schema(SERVICE_SET_ZWAVE_PARAMETER_SCHEMA),
        service_func=_async_set_zwave_parameter,
    )

    async def _async_get_zwave_parameter(call: ServiceCall) -> ServiceResponse:
        return await entity_service_call(
            hass,
            async_get_entities(hass, supports="async_get_zwave_parameter"),
            "async_get_zwave_parameter",
            call,
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_GET_ZWAVE_PARAMETER,
        schema=cv.make_entity_service_schema(SERVICE_GET_ZWAVE_PARAMETER_SCHEMA),
        service_func=_async_get_zwave_parameter,
        supports_response=SupportsResponse.OPTIONAL,
    )


# Compat shim — lock.py still imports this. The function is a no-op
# while Z-Wave user-code services are unsupported; lock.py keeps the
# call so the platform's setup_entry doesn't change shape later.
@callback
def async_setup_lock_services(hass: HomeAssistant) -> None:
    """No-op placeholder for Z-Wave lock services."""
