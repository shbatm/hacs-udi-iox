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

from tests.conftest import isy_data_for


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
    isy_data = isy_data_for(controller)
    assert _select_options(isy_data, node, PROP_RAMP_RATE) == RAMP_RATE_OPTIONS


async def test_select_options_falls_back_to_uom_to_states() -> None:
    """When the editor has no names, fall back to UOM_TO_STATES via the
    property's UOM."""
    from pyisyox.client import NodePropertyValue
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
    isy_data = isy_data_for(controller)

    options = _select_options(isy_data, node, "X")
    assert options  # non-empty
    assert all(isinstance(o, str) for o in options)


async def test_select_options_returns_empty_when_unresolvable() -> None:
    """No ramp-rate, no editor names, no UOM_TO_STATES match → empty list."""
    from pyisyox.client import NodePropertyValue
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
    isy_data = isy_data_for(controller)
    assert _select_options(isy_data, node, "Y") == []


async def test_ramp_rate_select_entity_round_trip() -> None:
    """current_option indexes into RAMP_RATE_OPTIONS by raw int; select
    sends back the index via set_ramp_rate."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox import Node
    from pyisyox.client import NodePropertyValue
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
    isy_data = isy_data_for(controller)
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
    from pyisyox.client import NodePropertyValue
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
    isy_data = isy_data_for(controller)
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
    isy_data = isy_data_for(controller)
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
    isy_data = isy_data_for(controller)
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
    isy_data = isy_data_for(controller)
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
    isy_data = isy_data_for(controller)
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

    from types import SimpleNamespace

    def _frame(value: object, cmd1: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            memory=BACKLIGHT_MEMORY_FILTER["memory"],
            cmd1=cmd1 if cmd1 is not None else BACKLIGHT_MEMORY_FILTER["cmd1"],
            value=value,
        )

    with (
        patch.object(entity, "_editor_range_for", return_value=editor_range),
        patch.object(ISYBacklightSelectEntity, "async_write_ha_state", lambda s: None),
    ):
        entity._on_memory_write(_frame(1))  # type: ignore[arg-type]
    assert entity.current_option == "On 1"

    # Wrong cmd1 → no change.
    with patch.object(entity, "_editor_range_for", return_value=editor_range):
        entity._on_memory_write(_frame(1, cmd1="DEAD"))  # type: ignore[arg-type]
    assert entity.current_option == "On 1"

    # Unmappable raw → ignored.
    with (
        patch.object(entity, "_editor_range_for", return_value=editor_range),
        patch.object(ISYBacklightSelectEntity, "async_write_ha_state", lambda s: None),
    ):
        entity._on_memory_write(_frame(99))  # type: ignore[arg-type]
    assert entity.current_option == "On 1"


async def test_index_select_resolves_via_editor_names() -> None:
    """When UOM isn't in UOM_TO_STATES, fall through to the editor's
    names table to map raw int → option string."""
    from unittest.mock import patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox.client import NodePropertyValue
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
    isy_data = isy_data_for(controller)
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


# --- Coverage push: setup branches and current_option fallbacks ---


async def test_select_options_uses_editor_names_with_subset() -> None:
    """When the resolved range has both ``names`` and a ``subset``, the
    options come from the subset keys mapped through names (line 66-67)."""
    from unittest.mock import MagicMock, patch

    from custom_components.udi_iox.select import _select_options

    fake_range = MagicMock(
        names={"0": "Off", "1": "Low", "2": "High"},
        subset={"0", "2"},
    )
    isy_data = MagicMock()
    node = MagicMock()
    with patch(
        "custom_components.udi_iox.select.range_for_control",
        return_value=fake_range,
    ):
        opts = _select_options(isy_data, node, "OL")
    # Sorted by raw int key: ["0","2"] → ["Off", "High"].
    assert opts == ["Off", "High"]


async def test_async_setup_entry_skips_aux_with_no_options(hass) -> None:
    """An aux SELECT control whose ``_select_options`` returns []
    (no editor names AND no UOM_TO_STATES match) is skipped (lines
    113-123)."""
    from unittest.mock import MagicMock

    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import async_setup_entry

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "X"), controller)
    isy_data = isy_data_for(controller)
    isy_data.aux_properties[Platform.SELECT].append((node, "UNKNOWN"))
    isy_data.devices[node.address] = MagicMock()
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    # No options resolvable → no entity created.
    assert collected == []


async def test_aux_index_select_async_added_restores_optimistic_state(hass) -> None:
    """A write-only aux select restores its last selected option
    on add (lines 178-184)."""
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import ISYAuxControlIndexSelectEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlIndexSelectEntity(
        isy_data=isy_data,
        node=node,
        control="UNKNOWN",  # write-only
        unique_id="x_set",
        description=SelectEntityDescription(key="UNKNOWN", options=["Lo", "Hi"]),
        device_info=None,
    )
    entity.hass = hass
    entity.entity_id = "select.x"
    last = MagicMock()
    last.state = "Hi"
    entity.async_get_last_state = AsyncMock(return_value=last)
    await entity.async_added_to_hass()
    assert entity._optimistic_option == "Hi"


async def test_async_setup_entry_creates_backlight_and_aux_index_entities(hass) -> None:
    """An aux-property CMD_BACKLIGHT control creates an
    ``ISYBacklightSelectEntity`` (lines 113-115); a UOM_TO_STATES
    control creates an ``ISYAuxControlIndexSelectEntity`` (line 123)."""
    from unittest.mock import MagicMock

    from pyisyox.client import NodePropertyValue
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import (
        ISYAuxControlIndexSelectEntity,
        ISYBacklightSelectEntity,
        async_setup_entry,
    )

    controller = make_controller(make_load_result())
    bl_node = make_node(make_node_record("AA AA AA 1", "X"), controller)
    # UOM 66 — HVAC heat/cool state, in UOM_TO_STATES → triggers options.
    aux = make_node_record(
        "AA AA AA 2",
        "Y",
        properties={
            "MY": NodePropertyValue(
                id="MY", value="0", formatted="Off", uom="66", name="MY"
            )
        },
    )
    aux_node = make_node(aux, controller)
    isy_data = isy_data_for(controller)
    isy_data.aux_properties[Platform.SELECT].extend(
        [(bl_node, CMD_BACKLIGHT), (aux_node, "MY")]
    )
    isy_data.devices[bl_node.address] = MagicMock()
    isy_data.devices[aux_node.address] = MagicMock()
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert any(isinstance(e, ISYBacklightSelectEntity) for e in collected)
    assert any(isinstance(e, ISYAuxControlIndexSelectEntity) for e in collected)


async def test_aux_index_select_current_option_uses_uom_to_states_lookup() -> None:
    """When the property's UOM is in ``UOM_TO_STATES``, the current
    option resolves through that map (line 196-197). Drive the readback
    path by patching ``_has_readback`` to True."""
    from unittest.mock import patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.select import ISYAuxControlIndexSelectEntity

    controller = make_controller(make_load_result())
    # UOM 66 → HVAC heat/cool state; "1" → some label.
    record = make_node_record(
        "A 1",
        "X",
        properties={
            "MY": NodePropertyValue(
                id="MY", value="1", formatted="On", uom="66", name="MyControl"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlIndexSelectEntity(
        isy_data=isy_data,
        node=node,
        control="MY",
        unique_id="x_my",
        description=SelectEntityDescription(key="MY", options=["a", "b"]),
        device_info=None,
    )
    with patch.object(
        ISYAuxControlIndexSelectEntity,
        "_has_readback",
        new_callable=lambda: property(lambda _self: True),
    ):
        # The UOM_TO_STATES["66"] maps "1" → some label (or returns the
        # raw value as fallback). Either way, current_option is non-None.
        assert entity.current_option is not None


async def test_backlight_select_async_added_restores_state_and_subscribes(hass) -> None:
    """``ISYBacklightSelectEntity.async_added_to_hass`` restores the
    last-known state and subscribes to memory-write echoes (lines
    255-265)."""
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.components.select import SelectEntityDescription
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
    isy_data = isy_data_for(controller)
    entity = ISYBacklightSelectEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=SelectEntityDescription(
            key=CMD_BACKLIGHT, options=["Off", "Low", "High"]
        ),
        device_info=None,
    )
    entity.hass = hass
    entity.entity_id = "select.bl"
    last = MagicMock()
    last.state = "Low"
    entity.async_get_last_state = AsyncMock(return_value=last)
    sub_calls: list[tuple] = []
    isy_data.controller_events.subscribe_node = (  # type: ignore[assignment]
        lambda addr, ctrl, cb: sub_calls.append((addr, ctrl)) or (lambda: None)
    )
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "Low"
    assert ("A 1", "_7M") in sub_calls or any(addr == "A 1" for addr, _ in sub_calls)


async def test_backlight_memory_write_skips_when_value_matches_current() -> None:
    """If the memory-write decodes to the option already selected, the
    handler bails out without firing async_write_ha_state (line 287)."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from homeassistant.components.select import SelectEntityDescription
    from pyisyox.constants import CMD_BACKLIGHT
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
    isy_data = isy_data_for(controller)
    entity = ISYBacklightSelectEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=SelectEntityDescription(key=CMD_BACKLIGHT, options=["0", "1"]),
        device_info=None,
    )
    entity._attr_current_option = "1"
    frame = SimpleNamespace(
        memory=BACKLIGHT_MEMORY_FILTER["memory"],
        cmd1=BACKLIGHT_MEMORY_FILTER["cmd1"],
        value=1,
    )
    write_calls: list[int] = []
    with patch.object(
        ISYBacklightSelectEntity,
        "async_write_ha_state",
        lambda s: write_calls.append(1),
    ):
        entity._on_memory_write(frame)  # type: ignore[arg-type]
    assert write_calls == []
