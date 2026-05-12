"""Tests for the udi_iox services.

Pins:
- ``set_variable`` routes through ``Controller.set_variable_value`` /
  ``set_variable_init`` based on the ``init`` flag — both land
  ``POST /api/variables/{type}/{id}`` on the client.
- ``system_query`` routes through ``Controller.refresh``.
- ``send_program_command`` resolves the target by id / name, then
  invokes the matching method on the typed ``Program`` /
  ``ProgramFolder`` wrapper (which fires
  ``GET /rest/programs/{id}/{command}`` on the client).
- ``run_network_resource`` routes by id straight through the
  controller helper, and by name through the typed
  ``NetworkResource.run()`` wrapper.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pyisyox import Controller

from custom_components.udi_iox.const import DOMAIN
from custom_components.udi_iox.models import IsyData
from custom_components.udi_iox.services import (
    SERVICE_GET_NODE_COMMANDS,
    SERVICE_RENAME_NODE,
    SERVICE_RUN_NETWORK_RESOURCE,
    SERVICE_SEND_NODE_COMMAND,
    SERVICE_SEND_PROGRAM_COMMAND,
    SERVICE_SET_VARIABLE,
    SERVICE_SYSTEM_QUERY,
    async_get_entities,
    async_setup_services,
)
from tests.builders import (
    make_controller,
    make_load_result,
    make_network_resource_record,
    make_node_record,
    make_program_record,
)


@pytest.fixture
def service_controller():
    """Real :class:`Controller` shaped for service-layer tests.

    The default load_result carries one program ("0030", "Switch"), one
    folder ("0001", "Container"), one network resource ("5", "Webhook"),
    and one node — enough to exercise the by-id / by-name resolution
    paths in ``send_program_command`` / ``run_network_resource`` without
    each test having to re-seed the controller.
    """
    program = make_program_record("0030", "Switch")
    folder = make_program_record("0001", "Container", is_folder=True)
    resource = make_network_resource_record("5", "Webhook")
    node = make_node_record("A 1", "Test Node")
    load_result = make_load_result(
        uuid="test-uuid",
        nodes={node.address: node},
        programs={program.address: program, folder.address: folder},
        network_resources={resource.address: resource},
    )
    return make_controller(load_result)


async def _wire_services_with_entry(hass: HomeAssistant, controller) -> None:
    """Register services + add a stub config entry whose runtime_data
    points at the Controller so service handlers can resolve it."""
    isy_data = IsyData()
    isy_data.root = controller

    entry = MagicMock()
    entry.runtime_data = isy_data
    hass.config_entries.async_entries = MagicMock(return_value=[entry])

    async_setup_services(hass)
    await hass.async_block_till_done()


# --- system_query -----------------------------------------------------


async def test_system_query_calls_controller_refresh(hass, service_controller) -> None:
    """``system_query`` lands on ``Controller.refresh``. Patched at the
    class level because ``Controller`` uses ``__slots__`` — we can't
    drop an ``AsyncMock`` on the instance directly."""
    await _wire_services_with_entry(hass, service_controller)

    with patch.object(
        Controller, "refresh", new=AsyncMock(return_value=None)
    ) as refresh_mock:
        await hass.services.async_call(DOMAIN, SERVICE_SYSTEM_QUERY, {}, blocking=True)

    refresh_mock.assert_awaited_once()


async def test_system_query_targets_controller_by_uuid(
    hass, service_controller
) -> None:
    """Passing isy=<uuid> targets only the matching controller."""
    await _wire_services_with_entry(hass, service_controller)

    with patch.object(
        Controller, "refresh", new=AsyncMock(return_value=None)
    ) as refresh_mock:
        await hass.services.async_call(
            DOMAIN, SERVICE_SYSTEM_QUERY, {"isy": "test-uuid"}, blocking=True
        )

    refresh_mock.assert_awaited_once()


async def test_system_query_with_unmatched_isy_raises(hass, service_controller) -> None:
    await _wire_services_with_entry(hass, service_controller)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SYSTEM_QUERY, {"isy": "no-such-uuid"}, blocking=True
        )


# --- set_variable -----------------------------------------------------


async def test_set_variable_value_writes_through_controller(
    hass, service_controller
) -> None:
    """``init=False`` (the default) maps to
    ``Controller.set_variable_value`` which lands
    ``POST /api/variables/{type}/{id}`` with ``{"value": <int>}``."""
    await _wire_services_with_entry(hass, service_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VARIABLE,
        {CONF_ADDRESS: 5, CONF_TYPE: 2, "value": 100},
        blocking=True,
    )

    assert service_controller._client.post_variable_update.await_args_list == [
        call(2, 5, {"value": 100})
    ]


async def test_set_variable_init_routes_to_init_method(
    hass, service_controller
) -> None:
    """``init=True`` maps to ``Controller.set_variable_init`` which
    lands the same endpoint with ``{"init": <int>}`` instead."""
    await _wire_services_with_entry(hass, service_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VARIABLE,
        {CONF_ADDRESS: 5, CONF_TYPE: 1, "value": 42, "init": True},
        blocking=True,
    )

    assert service_controller._client.post_variable_update.await_args_list == [
        call(1, 5, {"init": 42})
    ]


# --- send_program_command ---------------------------------------------


async def test_send_program_command_by_id_dispatches_via_wrapper(
    hass, service_controller
) -> None:
    """Targeting a program by id calls the matching method on the typed
    ``Program`` wrapper (for ``run_then`` that lands
    ``GET /rest/programs/{id}/runThen`` on the client)."""
    await _wire_services_with_entry(hass, service_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_PROGRAM_COMMAND,
        {CONF_ADDRESS: "0030", "command": "run_then"},
        blocking=True,
    )
    assert service_controller._client.run_program_command.await_args_list == [
        call("0030", "runThen")
    ]


async def test_send_program_command_by_name_resolves_then_dispatches(
    hass, service_controller
) -> None:
    """By-name lookup finds the program in
    ``controller.programs.values()`` and invokes the same wrapper
    method — same wire shape as the by-id path."""
    await _wire_services_with_entry(hass, service_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_PROGRAM_COMMAND,
        {CONF_NAME: "Switch", "command": "run_else"},
        blocking=True,
    )
    assert service_controller._client.run_program_command.await_args_list == [
        call("0030", "runElse")
    ]


async def test_send_program_command_unknown_id_raises(hass, service_controller) -> None:
    await _wire_services_with_entry(hass, service_controller)

    with pytest.raises(HomeAssistantError, match="No program or folder with id"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PROGRAM_COMMAND,
            {CONF_ADDRESS: "FFFF", "command": "run"},
            blocking=True,
        )


async def test_send_program_command_unknown_name_raises(
    hass, service_controller
) -> None:
    await _wire_services_with_entry(hass, service_controller)

    with pytest.raises(HomeAssistantError, match="No program or folder named"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PROGRAM_COMMAND,
            {CONF_NAME: "no-such", "command": "run"},
            blocking=True,
        )


async def test_send_program_command_folder_rejects_program_only_verb(
    hass, service_controller
) -> None:
    """``run_then`` is a Program-only verb. Targeting a folder with it
    raises a targeted ``HomeAssistantError`` instead of letting the
    request flow through to ``ProgramFolder`` (where ``run_then`` would
    raise ``AttributeError`` opaquely)."""
    await _wire_services_with_entry(hass, service_controller)

    with pytest.raises(HomeAssistantError, match="does not support command"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PROGRAM_COMMAND,
            {CONF_ADDRESS: "0001", "command": "run_then"},
            blocking=True,
        )


# --- run_network_resource --------------------------------------------


async def test_run_network_resource_by_address_fires_controller(
    hass, service_controller
) -> None:
    """Targeting a resource by id calls
    ``Controller.run_network_resource(<id>)`` directly — lands
    ``GET /rest/networking/resources/{id}`` on the client."""
    await _wire_services_with_entry(hass, service_controller)

    await hass.services.async_call(
        DOMAIN, SERVICE_RUN_NETWORK_RESOURCE, {CONF_ADDRESS: 5}, blocking=True
    )
    assert service_controller._client.run_network_resource.await_args_list == [
        call("5")
    ]


async def test_run_network_resource_by_name_resolves_then_fires(
    hass, service_controller
) -> None:
    """By-name resolution finds the wrapper in
    ``controller.network_resources`` and calls its ``run()`` —
    same ``GET /rest/networking/resources/{id}`` on the wire as the
    by-id path."""
    await _wire_services_with_entry(hass, service_controller)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_NETWORK_RESOURCE,
        {CONF_NAME: "Webhook"},
        blocking=True,
    )
    assert service_controller._client.run_network_resource.await_args_list == [
        call("5")
    ]


async def test_run_network_resource_unknown_id_raises(hass, service_controller) -> None:
    await _wire_services_with_entry(hass, service_controller)

    with pytest.raises(HomeAssistantError, match="No network resource with id"):
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_NETWORK_RESOURCE, {CONF_ADDRESS: 99}, blocking=True
        )


async def test_run_network_resource_unknown_name_raises(
    hass, service_controller
) -> None:
    await _wire_services_with_entry(hass, service_controller)

    with pytest.raises(HomeAssistantError, match="No network resource named"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_NETWORK_RESOURCE,
            {CONF_NAME: "no-such"},
            blocking=True,
        )


# --- entity-targeting service registration ---------------------------


async def test_rename_node_service_is_registered(hass, service_controller) -> None:
    """The ``rename_node`` HA entity service must register at setup
    time. End-to-end dispatch through ``entity_service_call`` is
    exercised at the entity layer (``ISYNodeEntity.async_rename_node``);
    here we just pin that the service is wired so HA can route to it."""
    await _wire_services_with_entry(hass, service_controller)
    assert hass.services.has_service(DOMAIN, SERVICE_RENAME_NODE)
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_NODE_COMMAND)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_NODE_COMMANDS)


async def test_async_get_entities_returns_mapping(hass, service_controller) -> None:
    """``entity_service_call`` needs a ``dict[str, Entity]`` — passing the
    raw platform list (the pre-fix behavior) raises ``AttributeError`` at
    dispatch. Pin that the helper hands back a mapping."""
    await _wire_services_with_entry(hass, service_controller)
    assert isinstance(async_get_entities(hass), dict)


# --- accepted-command surface (get_node_commands + JIT validation) ---


def _bare_node_entity(node):
    """An ``ISYNodeEntity`` with just ``_node`` wired — enough for the
    nodedef-reading helpers without a full HA setup."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node
    return entity


async def test_get_node_commands_returns_id_to_name_map(service_controller) -> None:
    """``async_get_node_commands`` returns an id→friendly-name mapping,
    sorted by wire id, covering exactly the nodedef accept set."""
    node = next(iter(service_controller.nodes.values()))
    entity = _bare_node_entity(node)

    result = await entity.async_get_node_commands()
    commands = result["accepted_commands"]

    accepts = node.nodedef.cmds.accepts
    assert set(commands) == {c.id for c in accepts}
    assert commands == {
        c.id: c.name or c.id for c in sorted(accepts, key=lambda c: c.id)
    }
    assert list(commands) == sorted(commands)


async def test_send_node_command_rejects_unaccepted_verb(service_controller) -> None:
    """A verb absent from the node's nodedef accept set raises before
    any controller round-trip."""
    node = next(iter(service_controller.nodes.values()))
    assert "NOT_A_REAL_COMMAND" not in {c.id for c in node.nodedef.cmds.accepts}
    entity = _bare_node_entity(node)

    with pytest.raises(ServiceValidationError):
        await entity.async_send_node_command("NOT_A_REAL_COMMAND")
    service_controller._client.post_node_update.assert_not_awaited()


async def test_send_node_command_accepted_verb_passes_through(
    service_controller,
) -> None:
    """An accepted verb is forwarded to ``Node.send_command``."""
    node = next(iter(service_controller.nodes.values()))
    entity = _bare_node_entity(node)

    assert "BEEP" in {c.id for c in node.nodedef.cmds.accepts}
    with patch.object(type(node), "send_command", new=AsyncMock()) as send:
        # Friendly "beep" → wire id "BEEP".
        await entity.async_send_node_command("beep")

    send.assert_awaited_once_with("BEEP")


async def test_validate_command_skipped_when_nodedef_unresolved(
    service_controller,
) -> None:
    """No nodedef → can't validate; the verb is let through (the
    controller will reject it if truly bogus)."""
    node = next(iter(service_controller.nodes.values()))
    entity = _bare_node_entity(node)

    with (
        patch.object(type(node), "nodedef", property(lambda self: None)),
        patch.object(type(node), "send_command", new=AsyncMock()) as send,
    ):
        await entity.async_send_node_command("anything")

    send.assert_awaited_once_with("anything")


# --- entity rename plumbing ------------------------------------------


async def test_isy_node_entity_async_rename_calls_node_rename(
    service_controller,
) -> None:
    """``ISYNodeEntity.async_rename_node`` calls ``node.rename(name)``,
    which lands ``POST /api/nodes/{addr}`` with
    ``{"nodeType": "node", "name": "..."}`` on the client."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    node = next(iter(service_controller.nodes.values()))
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node

    await entity.async_rename_node("Renamed")

    assert service_controller._client.post_node_update.await_args_list == [
        call("A 1", {"nodeType": "node", "name": "Renamed"})
    ]
