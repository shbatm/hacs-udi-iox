"""Tests for the udi_iox services.

Pins:
- ``set_variable`` routes through ``Controller.set_variable_value`` /
  ``set_variable_init`` based on the ``init`` flag.
- ``system_query`` routes through ``Controller.refresh``.
- The deferred services (``send_program_command``,
  ``run_network_resource``) raise so callers know they're not wired
  rather than silently no-op'ing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.udi_iox.const import DOMAIN
from custom_components.udi_iox.services import (
    SERVICE_RENAME_NODE,
    SERVICE_RUN_NETWORK_RESOURCE,
    SERVICE_SEND_NODE_COMMAND,
    SERVICE_SEND_PROGRAM_COMMAND,
    SERVICE_SET_VARIABLE,
    SERVICE_SYSTEM_QUERY,
    async_setup_services,
)
from custom_components.udi_iox.models import IsyData


async def _wire_services_with_entry(hass: HomeAssistant, fake_controller) -> None:
    """Register services + add a fake config entry whose runtime_data
    points at the FakeController so service handlers can resolve it."""
    # Build a stub config entry with runtime_data so
    # ``_select_isy_data`` finds it.
    isy_data = IsyData()
    isy_data.root = fake_controller

    entry = MagicMock()
    entry.runtime_data = isy_data
    # Patch the config-entries lookup so service handlers see our entry.
    hass.config_entries.async_entries = MagicMock(return_value=[entry])

    async_setup_services(hass)
    await hass.async_block_till_done()


# --- system_query -----------------------------------------------------


async def test_system_query_calls_controller_refresh(
    hass, fake_controller
) -> None:
    await _wire_services_with_entry(hass, fake_controller)

    await hass.services.async_call(DOMAIN, SERVICE_SYSTEM_QUERY, {}, blocking=True)

    assert fake_controller.refresh_calls == 1


async def test_system_query_targets_controller_by_uuid(
    hass, fake_controller
) -> None:
    """Passing isy=<uuid> targets only the matching controller."""
    await _wire_services_with_entry(hass, fake_controller)

    await hass.services.async_call(
        DOMAIN, SERVICE_SYSTEM_QUERY, {"isy": "test-uuid"}, blocking=True
    )
    assert fake_controller.refresh_calls == 1


async def test_system_query_with_unmatched_isy_raises(
    hass, fake_controller
) -> None:
    await _wire_services_with_entry(hass, fake_controller)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SYSTEM_QUERY, {"isy": "no-such-uuid"}, blocking=True
        )


# --- set_variable -----------------------------------------------------


async def test_set_variable_value_writes_through_controller(
    hass, fake_controller
) -> None:
    await _wire_services_with_entry(hass, fake_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VARIABLE,
        {CONF_ADDRESS: 5, CONF_TYPE: 2, "value": 100},
        blocking=True,
    )

    assert fake_controller.set_variable_value_calls == [(2, 5, 100)]
    assert fake_controller.set_variable_init_calls == []


async def test_set_variable_init_routes_to_init_method(
    hass, fake_controller
) -> None:
    """init=True routes to set_variable_init instead of set_variable_value."""
    await _wire_services_with_entry(hass, fake_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VARIABLE,
        {CONF_ADDRESS: 5, CONF_TYPE: 1, "value": 42, "init": True},
        blocking=True,
    )

    assert fake_controller.set_variable_init_calls == [(1, 5, 42)]
    assert fake_controller.set_variable_value_calls == []


# --- deferred services ------------------------------------------------


async def test_send_program_command_raises(hass, fake_controller) -> None:
    await _wire_services_with_entry(hass, fake_controller)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PROGRAM_COMMAND,
            {CONF_NAME: "My Program", "command": "run"},
            blocking=True,
        )


async def test_run_network_resource_raises(hass, fake_controller) -> None:
    await _wire_services_with_entry(hass, fake_controller)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_NETWORK_RESOURCE,
            {CONF_ADDRESS: 1},
            blocking=True,
        )


# --- entity-targeting service registration ---------------------------


async def test_rename_node_service_is_registered(hass, fake_controller) -> None:
    """The ``rename_node`` HA entity service must register at setup
    time. End-to-end dispatch through ``entity_service_call`` is
    exercised at the entity layer (``ISYNodeEntity.async_rename_node``);
    here we just pin that the service is wired so HA can route to it."""
    await _wire_services_with_entry(hass, fake_controller)
    assert hass.services.has_service(DOMAIN, SERVICE_RENAME_NODE)
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_NODE_COMMAND)


# --- entity rename plumbing ------------------------------------------


async def test_isy_node_entity_async_rename_calls_node_rename(
    fake_node_factory,
) -> None:
    """``ISYNodeEntity.async_rename_node`` calls ``node.rename(name)``
    (which the runtime ``Node`` then routes to
    ``POST /api/nodes/{addr}`` with ``nodeType: "node"``)."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    node = fake_node_factory(address="A 1")
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node

    await entity.async_rename_node("Renamed")

    assert node.rename_calls == ["Renamed"]
    assert node.name == "Renamed"
