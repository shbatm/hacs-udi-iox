"""Tests for the udi_iox services.

Pins:
- ``send_program_command`` resolves the target by id / name, then
  invokes the matching method on the typed ``Program`` /
  ``ProgramFolder`` wrapper (which fires
  ``GET /rest/programs/{id}/{command}`` on the client).

Variable writes are exercised by the native number platform
(``test_number.py``); per-resource fire-triggers by the button platform
(``test_button.py``). The domain-level ``set_variable`` /
``run_network_resource`` services were removed in favour of those
paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pyisyox.testing import (
    make_controller,
    make_load_result,
    make_network_resource_record,
    make_node_record,
    make_program_record,
)

from custom_components.udi_iox.const import DOMAIN
from custom_components.udi_iox.services import (
    SERVICE_GET_NODE_COMMANDS,
    SERVICE_GET_ZWAVE_PARAMETER,
    SERVICE_RENAME_NODE,
    SERVICE_SEND_NODE_COMMAND,
    SERVICE_SEND_PROGRAM_COMMAND,
    SERVICE_SET_ZWAVE_PARAMETER,
    async_get_entities,
    async_setup_services,
)
from tests.conftest import isy_data_for


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
    isy_data = isy_data_for(controller)

    entry = MagicMock()
    entry.runtime_data = isy_data
    hass.config_entries.async_entries = MagicMock(return_value=[entry])

    async_setup_services(hass)
    await hass.async_block_till_done()


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
    assert hass.services.has_service(DOMAIN, SERVICE_SET_ZWAVE_PARAMETER)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_ZWAVE_PARAMETER)


async def test_async_get_entities_returns_mapping(hass, service_controller) -> None:
    """``entity_service_call`` needs a ``dict[str, Entity]`` — passing the
    raw platform list (the pre-fix behavior) raises ``AttributeError`` at
    dispatch. Pin that the helper hands back a mapping."""
    await _wire_services_with_entry(hass, service_controller)
    assert isinstance(async_get_entities(hass), dict)


async def test_async_get_entities_filters_by_supported_attribute(
    hass, service_controller
) -> None:
    """``async_get_entities(supports=...)`` drops entities that don't
    expose the given method. Without this filter, calling a
    node-targeted service (``send_node_command``, ``get_node_commands``,
    ``set_zwave_parameter``) on a scene/group entity raises
    ``AttributeError`` deep inside HA's ``_handle_entity_call`` (the
    user-reported "'ISYGroupSwitchEntity' object has no attribute
    'async_get_node_commands'" trace)."""
    from homeassistant.helpers.entity import Entity

    class _NodeLike(Entity):
        entity_id = "switch.dimmer_1"

        async def async_get_node_commands(self) -> dict[str, str]:
            return {}

    class _GroupLike(Entity):
        entity_id = "switch.scene_1"

    await _wire_services_with_entry(hass, service_controller)

    with patch(
        "custom_components.udi_iox.services.async_get_platforms"
    ) as get_platforms:
        platform = MagicMock()
        platform.entities = {
            "switch.dimmer_1": _NodeLike(),
            "switch.scene_1": _GroupLike(),
        }
        get_platforms.return_value = [platform]

        all_entities = async_get_entities(hass)
        filtered = async_get_entities(hass, supports="async_get_node_commands")

    assert set(all_entities) == {"switch.dimmer_1", "switch.scene_1"}
    assert set(filtered) == {"switch.dimmer_1"}


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


# --- Z-Wave parameter entity service ----------------------------------


async def test_async_set_zwave_parameter_delegates_to_node(
    service_controller,
) -> None:
    """``ISYNodeEntity.async_set_zwave_parameter`` calls
    :meth:`pyisyox.Node.set_zwave_parameter` with the
    ``(number, value, size)`` triple as-is — pyisyox's helper picks the
    right wire path from the node's ``family_id`` itself."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    node = next(iter(service_controller.nodes.values()))
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node

    with patch.object(type(node), "set_zwave_parameter", new=AsyncMock()) as set_param:
        await entity.async_set_zwave_parameter(24, 1, 1)

    set_param.assert_awaited_once_with(24, 1, 1)


async def test_async_set_zwave_parameter_raises_homeassistanterror_on_non_zwave(
    service_controller,
) -> None:
    """A non-Z-Wave node's set_zwave_parameter raises ``NodeCommandError``
    in pyisyox; the entity wrapper surfaces that as ``HomeAssistantError``
    so HA's service dispatch can format it for the user."""
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NodeCommandError

    from custom_components.udi_iox.entity import ISYNodeEntity

    node = next(iter(service_controller.nodes.values()))
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node

    with (
        patch.object(
            type(node),
            "set_zwave_parameter",
            new=AsyncMock(side_effect=NodeCommandError("not a Z-Wave node")),
        ),
        pytest.raises(HomeAssistantError, match="not a Z-Wave node"),
    ):
        await entity.async_set_zwave_parameter(24, 1, 1)


async def test_async_get_zwave_parameter_returns_parsed_dict(
    service_controller,
) -> None:
    """``async_get_zwave_parameter`` surfaces pyisyox's structured
    ``{"parameter", "size", "value"}`` return as the HA service
    response — matching PyISY 3.x's shape so existing automations
    migrating off the legacy integration keep the same keys."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    node = next(iter(service_controller.nodes.values()))
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node

    parsed = {"parameter": 24, "size": 1, "value": 2}
    with patch.object(
        type(node),
        "get_zwave_parameter",
        new=AsyncMock(return_value=parsed),
    ) as get_param:
        result = await entity.async_get_zwave_parameter(24)

    get_param.assert_awaited_once_with(24)
    assert result == parsed


async def test_valid_iox_command_validator() -> None:
    """``_valid_iox_command`` upper-cases and validates against the
    pyisyox COMMAND_FRIENDLY_NAME table."""
    import voluptuous as vol

    from custom_components.udi_iox.services import _valid_iox_command

    assert _valid_iox_command("don") == "DON"
    with pytest.raises(vol.Invalid, match="Unknown IoX command"):
        _valid_iox_command("not_a_real_cmd")


async def test_select_isy_data_skips_entries_with_none_runtime_data(
    hass, service_controller
) -> None:
    """An entry whose ``runtime_data`` is ``None`` (mid-setup) is
    silently skipped."""
    from custom_components.udi_iox.services import _select_isy_data

    none_entry = MagicMock()
    none_entry.runtime_data = None
    hass.config_entries.async_entries = MagicMock(return_value=[none_entry])
    assert list(_select_isy_data(hass, None)) == []


async def test_select_isy_data_filters_by_uuid(hass, service_controller) -> None:
    """``isy_name`` filters out controllers whose uuid doesn't match
    ."""
    from custom_components.udi_iox.services import _select_isy_data

    isy_data = isy_data_for(service_controller)
    entry = MagicMock()
    entry.runtime_data = isy_data
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    assert list(_select_isy_data(hass, "different-uuid")) == []
    matched = list(_select_isy_data(hass, "test-uuid"))
    assert len(matched) == 1


async def test_async_setup_services_no_op_when_already_registered(
    hass, service_controller
) -> None:
    """A second ``async_setup_services`` call is a no-op when one of the
    integration services is already registered."""
    await _wire_services_with_entry(hass, service_controller)
    # Second call should hit the early-return branch.
    async_setup_services(hass)


async def test_send_program_command_no_matching_controller_raises(
    hass, service_controller
) -> None:
    """A ``isy=`` argument that doesn't match any controller raises
    immediately rather than silently no-op'ing."""
    await _wire_services_with_entry(hass, service_controller)
    with pytest.raises(HomeAssistantError, match="No IoX controller matched"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_PROGRAM_COMMAND,
            {CONF_ADDRESS: "0030", "command": "run", "isy": "no-such-uuid"},
            blocking=True,
        )


async def test_entity_targeted_services_dispatch(hass, service_controller) -> None:
    """The five entity-targeted handlers each route through
    ``entity_service_call``."""
    from homeassistant.helpers.entity import Entity

    class _Stub(Entity):
        entity_id = "switch.x"

        async def async_send_node_command(self, **_):
            return None

        async def async_get_node_commands(self, **_):
            return {"accepted_commands": {}}

        async def async_rename_node(self, **_):
            return None

        async def async_set_zwave_parameter(self, **_):
            return None

        async def async_get_zwave_parameter(self, **_):
            return {"parameter": 1, "value": 0, "size": 1}

    await _wire_services_with_entry(hass, service_controller)
    stub = _Stub()
    stub.hass = hass
    with patch(
        "custom_components.udi_iox.services.async_get_platforms"
    ) as get_platforms:
        platform = MagicMock()
        platform.entities = {stub.entity_id: stub}
        get_platforms.return_value = [platform]
        # Each handler is registered against the same entity-service
        # plumbing; calling them all confirms each lambda gets invoked.
        with patch(
            "custom_components.udi_iox.services.entity_service_call",
            new=AsyncMock(return_value={"r": 1}),
        ) as esc:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SEND_NODE_COMMAND,
                {"entity_id": stub.entity_id, "command": "beep"},
                blocking=True,
            )
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GET_NODE_COMMANDS,
                {"entity_id": stub.entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.services.async_call(
                DOMAIN,
                SERVICE_RENAME_NODE,
                {"entity_id": stub.entity_id, "name": "n"},
                blocking=True,
            )
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_ZWAVE_PARAMETER,
                {
                    "entity_id": stub.entity_id,
                    "parameter": 1,
                    "value": 0,
                    "size": 1,
                },
                blocking=True,
            )
            await hass.services.async_call(
                DOMAIN,
                SERVICE_GET_ZWAVE_PARAMETER,
                {"entity_id": stub.entity_id, "parameter": 1},
                blocking=True,
                return_response=True,
            )
    assert esc.await_count == 5


async def test_send_node_command_translates_node_command_error(
    service_controller,
) -> None:
    """A controller-side rejection on the service surface becomes
    HomeAssistantError (was previously unhandled, surfacing as a raw
    NodeCommandError to the calling automation)."""
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NodeCommandError

    node = next(iter(service_controller.nodes.values()))
    entity = _bare_node_entity(node)

    assert "BEEP" in {c.id for c in node.nodedef.cmds.accepts}
    with patch.object(
        type(node),
        "send_command",
        new=AsyncMock(side_effect=NodeCommandError("nope")),
    ):
        with pytest.raises(HomeAssistantError, match="Unable to send BEEP"):
            await entity.async_send_node_command("beep")
        with pytest.raises(HomeAssistantError, match="Unable to send BEEP"):
            await entity.async_send_raw_node_command("BEEP")
