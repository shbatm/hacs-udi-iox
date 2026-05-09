"""IoX services and commands.

What's wired up vs. what isn't, and why:

* ``send_node_command`` — alive. Routes friendly names ("brighten",
  "fast_off", ...) into ``Node.send_command(cmd_id)`` via the
  ``ISYNodeEntity.async_send_node_command`` method. Editor-codec
  validated by pyisyox.
* ``set_variable`` — alive. Writes through
  :meth:`pyisyox.Controller.set_variable_value` /
  :meth:`set_variable_init`.
* ``system_query`` — alive. Calls
  :meth:`pyisyox.Controller.refresh` to re-pull the node table and
  reset cached state.
* ``send_program_command`` — stub. Programs are exposed as raw
  dicts in pyisyox 6.0.0a1 and the controller has no
  ``send_program_command`` method yet. Service raises
  HomeAssistantError so service-call traces stay obvious.
* ``run_network_resource`` — stub. /rest/networking has no typed
  pyisyox wrapper yet (see fork plan §Deferred).

Removed since the v3 surface is gone:
* ``send_raw_node_command`` — superseded by ``send_node_command``
  (every command on Node.send_command is editor-codec validated).
* ``rename_node`` — pyisyox doesn't expose a rename helper.
* ``get_zwave_parameter`` / ``set_zwave_parameter`` — Z-Wave wire
  surface deferred until a live capture lands.
* ``set_zwave_lock_user_code`` / ``delete_zwave_lock_user_code`` —
  same Z-Wave deferral; the lock platform's
  ``async_register_entity_service`` registration is also dropped
  in lock.py.
* ``cleanup_entities`` — never had a handler; HA's entity registry
  cleanup now runs automatically in ``util._async_cleanup_registry_entries``.
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
from pyisyox.constants import COMMAND_FRIENDLY_NAME

from .const import _LOGGER, DOMAIN

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

# Entity-targeting service (light, switch, climate, fan, cover, lock, etc.)
SERVICE_SEND_NODE_COMMAND = "send_node_command"

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
VALID_PROGRAM_COMMANDS = [
    "run",
    "run_then",
    "run_else",
    "stop",
    "enable",
    "disable",
    "enable_run_at_startup",
    "disable_run_at_startup",
]


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
        """Stub: programs are deferred in pyisyox 6.0.0a1."""
        raise HomeAssistantError(
            "Program command service is not supported in this release;"
            " typed program wrappers are deferred in pyisyox 6.0.0a1"
        )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SEND_PROGRAM_COMMAND,
        service_func=async_send_program_command,
        schema=SERVICE_SEND_PROGRAM_COMMAND_SCHEMA,
    )

    async def async_run_network_resource(call: ServiceCall) -> None:
        """Stub: /rest/networking has no typed wrapper yet."""
        raise HomeAssistantError(
            "Network resource service is not supported in this release"
        )

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


# Compat shim: lock.py still imports this. Z-Wave lock services are
# deferred — the function is intentionally a no-op so the lock platform
# can keep its setup_entry call site unchanged until Z-Wave lands.
@callback
def async_setup_lock_services(hass: HomeAssistant) -> None:
    """No-op while Z-Wave lock services are deferred."""
    _LOGGER.debug(
        "Z-Wave lock services (set_zwave_lock_user_code, delete_zwave_lock_user_code)"
        " are not registered in this release"
    )
