"""Snapshot tests for the udi_iox select platform."""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
)


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.SELECT]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_select_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every select entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


# --- Direct entity tests (cover up the non-snapshot logic) ---


def _isy_data_with(controller):
    from custom_components.udi_iox.models import IsyData

    data = IsyData()
    data.root = controller
    return data


async def test_select_options_ramp_rate_path() -> None:
    """PROP_RAMP_RATE always returns the bespoke ramp-rate option list."""
    from pyisyox.constants import PROP_RAMP_RATE
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import RAMP_RATE_OPTIONS, _select_options

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = _isy_data_with(controller)
    assert _select_options(isy_data, node, PROP_RAMP_RATE) == RAMP_RATE_OPTIONS


async def test_select_options_falls_back_to_uom_to_states() -> None:
    """When the editor has no names, fall back to UOM_TO_STATES via the
    property's UOM."""
    from pyisyox import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import _select_options

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Switch",
        properties={
            # UOM 67 is HVAC modes — a known entry in UOM_TO_STATES.
            "X": NodePropertyValue(
                id="X", value="0", formatted="Off", uom="67", name="X"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = _isy_data_with(controller)

    options = _select_options(isy_data, node, "X")
    assert options  # non-empty
    assert all(isinstance(o, str) for o in options)


async def test_select_options_returns_empty_when_unresolvable() -> None:
    """No ramp-rate, no editor names, no UOM_TO_STATES match → empty list."""
    from pyisyox import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import _select_options

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Switch",
        properties={
            "Y": NodePropertyValue(
                id="Y", value="0", formatted="?", uom="999", name="Y"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = _isy_data_with(controller)
    assert _select_options(isy_data, node, "Y") == []


async def test_ramp_rate_select_entity_round_trip() -> None:
    """current_option indexes into RAMP_RATE_OPTIONS by raw int; select
    sends back the index via set_ramp_rate."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox import Node, NodePropertyValue
    from pyisyox.constants import PROP_RAMP_RATE
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import (
        RAMP_RATE_OPTIONS,
        ISYRampRateSelectEntity,
    )

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Dimmer",
        properties={
            "RR": NodePropertyValue(
                id="RR", value="3", formatted="", uom="57", name="Ramp Rate"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = _isy_data_with(controller)
    entity = ISYRampRateSelectEntity(
        isy_data=isy_data,
        node=node,
        control=PROP_RAMP_RATE,
        unique_id="x_rr",
        description=SelectEntityDescription(key="rr", options=RAMP_RATE_OPTIONS),
        device_info=None,
    )
    assert entity.current_option == RAMP_RATE_OPTIONS[3]

    set_ramp_rate = AsyncMock()
    with patch.object(Node, "set_ramp_rate", new=set_ramp_rate):
        await entity.async_select_option(RAMP_RATE_OPTIONS[5])
    set_ramp_rate.assert_awaited_once_with(5)


async def test_ramp_rate_select_handles_unknown_state() -> None:
    """A missing / unparsable RR value yields current_option None."""
    from homeassistant.components.select import SelectEntityDescription
    from pyisyox import NodePropertyValue
    from pyisyox.constants import PROP_RAMP_RATE
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import (
        RAMP_RATE_OPTIONS,
        ISYRampRateSelectEntity,
    )

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Dimmer",
        properties={
            "RR": NodePropertyValue(
                id="RR", value="not_a_num", formatted="", uom="57", name="Ramp Rate"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = _isy_data_with(controller)
    entity = ISYRampRateSelectEntity(
        isy_data=isy_data,
        node=node,
        control=PROP_RAMP_RATE,
        unique_id="x_rr",
        description=SelectEntityDescription(key="rr", options=RAMP_RATE_OPTIONS),
        device_info=None,
    )
    assert entity.current_option is None


async def test_aux_index_select_writeonly_optimistic_round_trip() -> None:
    """A control without a backing nodedef property: option restored on
    add, then write goes through send_command + optimistic update."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox import Node
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import ISYAuxControlIndexSelectEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = _isy_data_with(controller)
    entity = ISYAuxControlIndexSelectEntity(
        isy_data=isy_data,
        node=node,
        control="CUSTOM",
        unique_id="x_c",
        description=SelectEntityDescription(key="c", options=["A", "B"]),
        device_info=None,
    )
    assert entity.assumed_state is True
    assert entity.current_option is None  # not set yet

    send_command = AsyncMock()
    with (
        patch.object(Node, "send_command", new=send_command),
        patch.object(
            ISYAuxControlIndexSelectEntity, "async_write_ha_state", lambda s: None
        ),
    ):
        await entity.async_select_option("B")
    send_command.assert_awaited_once_with("CUSTOM", "B")
    assert entity.current_option == "B"


async def test_aux_index_select_translates_node_command_error() -> None:
    """A rejection on the wire write becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.select import SelectEntityDescription
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import ISYAuxControlIndexSelectEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = _isy_data_with(controller)
    entity = ISYAuxControlIndexSelectEntity(
        isy_data=isy_data,
        node=node,
        control="CUSTOM",
        unique_id="x_c",
        description=SelectEntityDescription(key="c", options=["A", "B"]),
        device_info=None,
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Could not set"),
    ):
        await entity.async_select_option("A")


async def test_backlight_select_translates_error_and_updates_option() -> None:
    """Successful set_backlight updates current_option; failure raises."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.select import SelectEntityDescription
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import ISYBacklightSelectEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = _isy_data_with(controller)
    entity = ISYBacklightSelectEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=SelectEntityDescription(
            key=CMD_BACKLIGHT, options=["Off", "On 50%"]
        ),
        device_info=None,
    )
    set_backlight = AsyncMock()
    with (
        patch.object(Node, "set_backlight", new=set_backlight),
        patch.object(ISYBacklightSelectEntity, "async_write_ha_state", lambda s: None),
    ):
        await entity.async_select_option("On 50%")
    set_backlight.assert_awaited_once_with("On 50%")
    assert entity.current_option == "On 50%"

    with (
        patch.object(
            Node, "set_backlight", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Could not set backlight"),
    ):
        await entity.async_select_option("Off")


async def test_backlight_memory_write_filter() -> None:
    """Memory writes matching the backlight filter (with a valid raw value)
    update current_option."""
    from unittest.mock import patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.schema.editor import EditorRange
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.const import BACKLIGHT_MEMORY_FILTER
    from custom_components.udi_iox.select import ISYBacklightSelectEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = _isy_data_with(controller)
    entity = ISYBacklightSelectEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=SelectEntityDescription(
            key=CMD_BACKLIGHT, options=["Off", "On 1", "On 2"]
        ),
        device_info=None,
    )

    editor_range = EditorRange(
        uom="25", min=0, max=2, names={0: "Off", 1: "On 1", 2: "On 2"}
    )

    class StubEvent:
        memory = BACKLIGHT_MEMORY_FILTER["memory"]
        cmd1 = BACKLIGHT_MEMORY_FILTER["cmd1"]
        value = 1

    with (
        patch.object(entity, "_editor_range_for", return_value=editor_range),
        patch.object(ISYBacklightSelectEntity, "async_write_ha_state", lambda s: None),
    ):
        entity._on_memory_write(StubEvent())  # type: ignore[arg-type]
    assert entity.current_option == "On 1"

    # Wrong cmd1 → no change.
    StubEvent.cmd1 = "DEAD"
    with patch.object(entity, "_editor_range_for", return_value=editor_range):
        entity._on_memory_write(StubEvent())  # type: ignore[arg-type]
    assert entity.current_option == "On 1"

    # Unmappable raw → ignored.
    StubEvent.cmd1 = BACKLIGHT_MEMORY_FILTER["cmd1"]
    StubEvent.value = 99
    with (
        patch.object(entity, "_editor_range_for", return_value=editor_range),
        patch.object(ISYBacklightSelectEntity, "async_write_ha_state", lambda s: None),
    ):
        entity._on_memory_write(StubEvent())  # type: ignore[arg-type]
    assert entity.current_option == "On 1"


async def test_index_select_resolves_via_editor_names() -> None:
    """When UOM isn't in UOM_TO_STATES, fall through to the editor's
    names table to map raw int → option string."""
    from unittest.mock import patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox import NodePropertyValue
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.editor import EditorRange
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import ISYAuxControlIndexSelectEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Lamp",
        properties={
            "MD": NodePropertyValue(
                id="MD", value="2", formatted="", uom="999", name="Mode"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = _isy_data_with(controller)
    nodedef = NodeDef(
        id="X",
        family_id="1",
        instance_id="1",
        properties={"MD": NodeProperty(id="MD", editor_id="MODE_ED")},
        cmds=NodeCommands(accepts=[Command(id="MD")]),
    )
    editor_range = EditorRange(uom="999", names={0: "Off", 1: "Low", 2: "High"})
    entity = ISYAuxControlIndexSelectEntity(
        isy_data=isy_data,
        node=node,
        control="MD",
        unique_id="x_md",
        description=SelectEntityDescription(key="md", options=["Off", "Low", "High"]),
        device_info=None,
    )
    with (
        patch.object(
            type(node), "nodedef", new_callable=lambda: property(lambda _s: nodedef)
        ),
        patch.object(entity, "_editor_range_for", return_value=editor_range),
    ):
        assert entity._has_readback is True
        assert entity.current_option == "High"
