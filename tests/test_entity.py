"""Direct unit tests for udi_iox/entity.py shared scaffolding."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from pyisyox.client import NodePropertyValue
from pyisyox.constants import ISY_VALUE_UNKNOWN
from pyisyox.testing import (
    make_controller,
    make_load_result,
    make_node,
    make_node_record,
)

from custom_components.udi_iox.entity import (
    _strip_parent_prefix,
    aux_entity_category,
    node_status_int,
)
from tests.conftest import isy_data_for


def test_aux_entity_category_st_is_primary_control() -> None:
    """A coalesced control that writes the node's status (``ST`` — a
    node-server ``virtualtemp`` setpoint, an i3 flags ``GV0`` "Mode")
    is the device's main control: no category. Every other aux control
    is configuration."""
    assert aux_entity_category("ST") is None
    assert aux_entity_category("GV1") is EntityCategory.CONFIG
    assert aux_entity_category("OL") is EntityCategory.CONFIG
    assert aux_entity_category("enabled") is EntityCategory.CONFIG


def test_node_status_int_returns_none_on_unknown_value() -> None:
    """``node.status.value == ISY_VALUE_UNKNOWN`` short-circuits to None."""
    fake = MagicMock()
    fake.status = NodePropertyValue(
        id="ST", value=ISY_VALUE_UNKNOWN, formatted="?", uom="0", name="Status"
    )
    assert node_status_int(fake) is None


def test_node_status_int_returns_none_on_unparseable_value() -> None:
    """A non-numeric ``value`` returns None."""
    fake = MagicMock()
    fake.status = NodePropertyValue(
        id="ST", value="banana", formatted="?", uom="0", name="Status"
    )
    assert node_status_int(fake) is None


def test_strip_parent_prefix_removes_label_prefix() -> None:
    """Sub-node labels prefixed with the parent name get the prefix
    stripped so HA's auto-name composition (parent + this) doesn't
    duplicate it."""
    assert _strip_parent_prefix("Hallway Light Button A", "Hallway Light") == "Button A"
    # No common prefix → return as-is.
    assert _strip_parent_prefix("Foo Bar", "Other") == "Foo Bar"
    # No parent → return as-is.
    assert _strip_parent_prefix("Foo Bar", None) == "Foo Bar"


def test_isy_entity_unavailable_when_ws_disconnected() -> None:
    """``ISYEntity.available`` returns False when the WS is down."""
    from custom_components.udi_iox.entity import ISYEntity

    controller = make_controller(make_load_result())
    make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    isy_data.controller_events.ws_connected = False  # type: ignore[attr-defined]

    entity = ISYEntity.__new__(ISYEntity)
    entity._isy_data = isy_data  # type: ignore[attr-defined]
    entity._attr_available = True
    assert entity.available is False


def test_isy_entity_address_falls_back_to_dict_keys() -> None:
    """For a raw dict node (legacy program / variable record), ``address``
    falls back to ``dict.get('address', dict.get('id', ''))``."""
    from custom_components.udi_iox.entity import ISYEntity

    entity = ISYEntity.__new__(ISYEntity)
    entity._node = {"address": "0010"}  # type: ignore[attr-defined]
    assert entity._node_address() == "0010"
    entity._node = {"id": "var-id"}  # type: ignore[attr-defined]
    assert entity._node_address() == "var-id"


def test_isy_node_entity_async_on_update_marks_available_and_writes() -> None:
    """``ISYNodeEntity.async_on_update`` refreshes ``_attr_available``
    from ``node.enabled`` and triggers a state write."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._isy_data = isy_data  # type: ignore[attr-defined]
    entity._node = node  # type: ignore[attr-defined]
    entity._attr_available = False  # type: ignore[attr-defined]
    write_calls: list[int] = []
    with patch.object(
        ISYNodeEntity, "async_write_ha_state", lambda s: write_calls.append(1)
    ):
        entity.async_on_update(None, "")  # type: ignore[arg-type]
    assert entity._attr_available == node.enabled
    assert write_calls == [1]


def test_isy_program_entity_async_on_update_writes_state() -> None:
    """``ISYProgramEntity._on_program_status`` calls
    ``async_write_ha_state``."""
    from custom_components.udi_iox.entity import ISYProgramEntity

    entity = ISYProgramEntity.__new__(ISYProgramEntity)
    write_calls: list[int] = []
    with patch.object(
        ISYProgramEntity, "async_write_ha_state", lambda s: write_calls.append(1)
    ):
        # The handler signature accepts a single event arg.
        entity._on_program_status(MagicMock())  # type: ignore[arg-type]
    assert write_calls == [1]


def test_isy_node_entity_lifecycle_handler_filters_other_addresses() -> None:
    """``_on_lifecycle`` ignores frames for unrelated addresses /
    actions."""
    from pyisyox import NodeLifecycleAction, NodeLifecycleEvent

    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._isy_data = isy_data  # type: ignore[attr-defined]
    entity._node = node  # type: ignore[attr-defined]
    entity._attr_available = False  # type: ignore[attr-defined]
    write_calls: list[int] = []
    with patch.object(
        ISYNodeEntity, "async_write_ha_state", lambda s: write_calls.append(1)
    ):
        # Wrong address.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED,
                node_address="OTHER",
                raw_action="NE",
                seqnum=1,
            )
        )
        # Right address, wrong action.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_RENAMED,
                node_address="A 1",
                raw_action="NN",
                seqnum=2,
            )
        )
        # Match.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED,
                node_address="A 1",
                raw_action="NE",
                seqnum=3,
            )
        )
    assert write_calls == [1]


def test_aux_command_name_uses_nodedef_command_name() -> None:
    """An aux entity whose ``control`` is a command (not a property) —
    e.g. a plugin's ``SETOL`` setter — takes its name from the
    controller-published ``cmds.accepts[].name`` ("Set On Level"),
    not a title-cased id ("Setol"). Regression for a virtualgeneric
    node-server node."""
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef

    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("n001_1", "Virtual Generic"), controller)
    isy_data = isy_data_for(controller)
    nodedef = NodeDef(
        id="virtualgeneric",
        family_id="1",
        instance_id="1",
        cmds=NodeCommands(accepts=[Command(id="SETOL", name="Set On Level")]),
    )
    with patch.object(
        type(node), "nodedef", new_callable=lambda: property(lambda _s: nodedef)
    ):
        entity = ISYNodeEntity(isy_data, node, control="SETOL", unique_id="x_setol")
    assert entity._attr_name == "Set On Level"


def test_aux_command_name_falls_back_when_nodedef_unnamed() -> None:
    """A command absent from the nodedef (or with an empty name) still
    falls back to the COMMAND_FRIENDLY_NAME / title-cased path so an
    unresolved nodedef doesn't blank the entity name."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("n001_1", "Virtual Generic"), controller)
    isy_data = isy_data_for(controller)
    # node.nodedef is None (no profile entry for this fixture) → fallback.
    entity = ISYNodeEntity(isy_data, node, control="SETOL", unique_id="x_setol")
    assert entity._attr_name == "Setol"


async def test_isy_entity_get_zwave_parameter_translates_node_command_error() -> None:
    """``async_get_zwave_parameter`` wraps NodeCommandError in
    HomeAssistantError."""
    from unittest.mock import AsyncMock

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NodeCommandError

    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node  # type: ignore[attr-defined]
    with (
        patch.object(
            type(node),
            "get_zwave_parameter",
            new=AsyncMock(side_effect=NodeCommandError("nope")),
        ),
        pytest.raises(HomeAssistantError, match="nope"),
    ):
        await entity.async_get_zwave_parameter(1)
