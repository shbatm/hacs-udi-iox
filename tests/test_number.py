"""Snapshot tests for the udi_iox number platform."""

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
    return [Platform.NUMBER]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_number_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every number entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_aux_on_level_uses_editor_units_both_directions() -> None:
    """``OL`` is the ``I_OL`` editor's UOM-51 0-100% quantity. A classic
    Insteon dimmer reports it as the raw UOM-100 0-255 byte on the wire,
    but pyisyox normalises that to the percentage on read — so the entity
    surfaces ``60`` for byte ``153`` — and writes the percentage straight
    through (``set_on_level(75)`` → pyisyox sends ``/cmd/OL/75/51``)."""
    from unittest.mock import AsyncMock, patch

    from pyisyox.client import NodePropertyValue
    from pyisyox.constants import PROP_ON_LEVEL
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import CONTROL_DESC, ISYAuxControlNumberEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "AA AA AA 1",
        "Lamp",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="0", formatted="Off", uom="100", name="Status"
            ),
            # Raw wire form: a 0-255 byte. pyisyox normalises to 60% (UOM 51).
            "OL": NodePropertyValue(
                id="OL", value="153", formatted="60%", uom="100", name="On Level"
            ),
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlNumberEntity(
        isy_data,
        node=node,
        control=PROP_ON_LEVEL,
        unique_id="x_OL",
        description=CONTROL_DESC[PROP_ON_LEVEL],
        device_info=None,
    )

    assert entity.native_value == 60  # byte 153 → 60%, normalised by pyisyox

    set_on_level = AsyncMock()
    with patch.object(type(node), "set_on_level", set_on_level):
        await entity.async_set_native_value(75)

    assert set_on_level.await_args.args == (75,)


async def test_variable_number_scales_by_precision_on_read_and_write() -> None:
    """IoX variables store a raw integer on the wire; ``precision``
    declares the implicit decimal shift.

    Read side: divide raw by ``10**precision`` — wire ``50`` with
    prec=1 surfaces as 5.0, not 50. Matches HA Core's isy994
    ``convert_isy_value_to_hass`` helper.

    Write side: the modern ``POST /api/variables/{type}/{id}``
    endpoint accepts ``float`` bodies and applies ``* 10**precision``
    server-side on store, so the displayed value passes through
    unchanged (the entity must NOT round to int — that would truncate
    the fraction and the controller would store the int verbatim
    without scaling, mismatching the displayed-unit contract).
    """
    from unittest.mock import AsyncMock, patch

    from pyisyox.client import VariableRecord
    from pyisyox.runtime import Variable
    from pyisyox.testing import make_controller, make_load_result

    from custom_components.udi_iox.number import ISYVariableNumberEntity

    controller = make_controller(make_load_result())
    record = VariableRecord(
        type_id="2",
        id="5",
        name="Temp",
        value=50,
        init=70,
        precision=1,
    )
    variable = Variable(record, controller._client)
    isy_data = isy_data_for(controller)

    from homeassistant.components.number import NumberEntityDescription

    desc = NumberEntityDescription(
        key="2.5",
        name="Temp",
        native_step=0.1,
        native_min_value=-1000,
        native_max_value=1000,
    )
    value_entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=desc,
        device_info=None,  # type: ignore[arg-type]
    )
    init_entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="vi",
        description=desc,
        device_info=None,  # type: ignore[arg-type]
        init_entity=True,
    )

    # Read side: raw 50 / 10**1 → 5.0; raw 70 → 7.0.
    assert value_entity.native_value == 5.0
    assert init_entity.native_value == 7.0

    # Write side: pass the displayed value through, rounded to int —
    # the controller multiplies by 10**precision server-side on store.
    # ``async_write_ha_state`` requires a live ``hass`` binding; patch
    # it out so the entity body runs in isolation.
    set_value = AsyncMock()
    set_init = AsyncMock()
    with (
        patch.object(type(variable), "set_value", set_value),
        patch.object(type(variable), "set_init", set_init),
        patch.object(
            ISYVariableNumberEntity, "async_write_ha_state", lambda self: None
        ),
    ):
        await value_entity.async_set_native_value(70.5)
        await init_entity.async_set_native_value(5)

    # Float passes through unchanged: pyisyox sends ``{"value": 70.5}``
    # and the controller applies ``* 10**precision`` on store, persisting
    # raw 705 (display 70.5). Rounding to int here would truncate the
    # fraction *and* misalign with the controller's precision math (int
    # bodies are stored verbatim with no scaling).
    assert set_value.await_args.args == (70.5,)
    # Integer input passes through as-is; pyisyox's _coerce_numeric
    # preserves the int and the controller stores 5 * 10 = 50 (display 5.0).
    assert set_init.await_args.args == (5,)


async def test_variable_number_pass_through_when_precision_zero() -> None:
    """A variable with ``precision=0`` round-trips raw ints — no
    scaling. Catches the obvious regression where the precision math
    runs unconditionally and shifts an integer-only variable."""
    from unittest.mock import AsyncMock, patch

    from pyisyox.client import VariableRecord
    from pyisyox.runtime import Variable
    from pyisyox.testing import make_controller, make_load_result

    from custom_components.udi_iox.number import ISYVariableNumberEntity

    controller = make_controller(make_load_result())
    record = VariableRecord(
        type_id="1", id="3", name="Count", value=42, init=0, precision=0
    )
    variable = Variable(record, controller._client)
    isy_data = isy_data_for(controller)

    from homeassistant.components.number import NumberEntityDescription

    desc = NumberEntityDescription(
        key="1.3", name="Count", native_step=1, native_min_value=0, native_max_value=255
    )
    entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=desc,
        device_info=None,  # type: ignore[arg-type]
    )

    assert entity.native_value == 42  # no scaling

    set_value = AsyncMock()
    with (
        patch.object(type(variable), "set_value", set_value),
        patch.object(
            ISYVariableNumberEntity, "async_write_ha_state", lambda self: None
        ),
    ):
        await entity.async_set_native_value(99)

    assert set_value.await_args.args == (99,)


# --- Coverage fillers: under-tested paths in number.py ---


async def test_number_description_fallback_without_editor() -> None:
    """No editor + no hand-tuned override → HA defaults (0-100, step 1)."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import _number_description

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)

    desc = _number_description(isy_data, node, "UNKNOWN_CONTROL")
    assert desc.native_min_value is None or desc.native_min_value == 0.0
    assert desc.native_max_value is None or desc.native_max_value == 100.0
    assert desc.entity_category is not None


async def test_aux_on_level_set_translates_node_command_error() -> None:
    """A failure on set_on_level becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.number import NumberEntityDescription
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.constants import PROP_ON_LEVEL
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYAuxControlNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlNumberEntity(
        isy_data=isy_data,
        node=node,
        control=PROP_ON_LEVEL,
        unique_id="x_ol",
        description=NumberEntityDescription(key=PROP_ON_LEVEL),
        device_info=None,
    )
    with (
        patch.object(
            Node, "set_on_level", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Could not set"),
    ):
        await entity.async_set_native_value(75)


async def test_aux_writeonly_control_uses_send_command_and_is_optimistic() -> None:
    """A control without a backing nodedef property is optimistic — read
    returns the last-set value, write goes through send_command + the
    optimistic value is updated."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox import Node
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYAuxControlNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlNumberEntity(
        isy_data=isy_data,
        node=node,
        control="CUSTOM",  # not in any nodedef property table
        unique_id="x_custom",
        description=NumberEntityDescription(key="CUSTOM"),
        device_info=None,
    )
    assert entity.assumed_state is True
    assert entity.native_value is None  # never set

    send_command = AsyncMock()
    with (
        patch.object(Node, "send_command", new=send_command),
        patch.object(ISYAuxControlNumberEntity, "async_write_ha_state", lambda s: None),
    ):
        await entity.async_set_native_value(42)
    send_command.assert_awaited_once_with("CUSTOM", 42)
    assert entity.native_value == 42  # optimistic update reflected


async def test_aux_writeonly_set_translates_node_command_error() -> None:
    """Optimistic-control rejection becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.number import NumberEntityDescription
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYAuxControlNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlNumberEntity(
        isy_data=isy_data,
        node=node,
        control="CUSTOM",
        unique_id="x_c",
        description=NumberEntityDescription(key="CUSTOM"),
        device_info=None,
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Could not set"),
    ):
        await entity.async_set_native_value(42)


async def test_aux_readback_handles_unparseable_value() -> None:
    """If the controller reports a non-numeric value for a readback
    control, native_value gracefully returns None (rather than raising)."""
    from unittest.mock import patch

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox.client import NodePropertyValue
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYAuxControlNumberEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Lamp",
        properties={
            "OL": NodePropertyValue(
                id="OL", value="not_a_number", formatted="?", uom="51", name="On Level"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    nodedef = NodeDef(
        id="X",
        family_id="1",
        instance_id="1",
        properties={"OL": NodeProperty(id="OL", editor_id="I_OL")},
        cmds=NodeCommands(accepts=[Command(id="OL")]),
    )
    entity = ISYAuxControlNumberEntity(
        isy_data=isy_data,
        node=node,
        control="OL",
        unique_id="x_ol",
        description=NumberEntityDescription(key="OL"),
        device_info=None,
    )
    with patch.object(
        type(node), "nodedef", new_callable=lambda: property(lambda _s: nodedef)
    ):
        assert entity._has_readback is True
        assert entity.native_value is None


async def test_variable_native_value_handles_none() -> None:
    """A variable without a value yields None (no division attempted)."""
    from homeassistant.components.number import NumberEntityDescription
    from pyisyox import Variable
    from pyisyox.testing import make_controller, make_load_result, make_variable_record

    from custom_components.udi_iox.number import ISYVariableNumberEntity

    controller = make_controller(make_load_result())
    record = make_variable_record("1", "1", "MyVar", precision=2)
    record.value = None  # type: ignore[assignment]
    variable = Variable(record, controller._client)
    isy_data = isy_data_for(controller)
    entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=NumberEntityDescription(key="v"),
        device_info=None,  # type: ignore[arg-type]
    )
    assert entity.native_value is None


async def test_variable_change_filter_routes_value_and_init_separately() -> None:
    """A current-value frame must not re-render the init-entity, and
    vice versa."""
    from unittest.mock import patch

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox import Variable
    from pyisyox.testing import make_controller, make_load_result, make_variable_record

    from custom_components.udi_iox.number import ISYVariableNumberEntity

    controller = make_controller(make_load_result())
    record = make_variable_record("1", "1", "MyVar")
    variable = Variable(record, controller._client)
    isy_data = isy_data_for(controller)
    value_entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=NumberEntityDescription(key="v"),
        device_info=None,  # type: ignore[arg-type]
        init_entity=False,
    )
    init_entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v_init",
        description=NumberEntityDescription(key="v_init"),
        device_info=None,  # type: ignore[arg-type]
        init_entity=True,
    )
    with patch.object(ISYVariableNumberEntity, "async_write_ha_state") as write:
        value_entity._on_variable_change(value=42, init=None)
        init_entity._on_variable_change(value=42, init=None)  # filtered
        assert write.call_count == 1
        write.reset_mock()
        value_entity._on_variable_change(value=None, init=10)  # filtered
        init_entity._on_variable_change(value=None, init=10)
        assert write.call_count == 1


async def test_variable_set_native_value_translates_error() -> None:
    """A set_value failure becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.number import NumberEntityDescription
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Variable
    from pyisyox.testing import make_controller, make_load_result, make_variable_record

    from custom_components.udi_iox.number import ISYVariableNumberEntity

    controller = make_controller(make_load_result())
    variable = Variable(make_variable_record("1", "1", "MyVar"), controller._client)
    isy_data = isy_data_for(controller)
    entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=NumberEntityDescription(key="v"),
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Variable, "set_value", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Could not set variable"),
    ):
        await entity.async_set_native_value(1)


async def test_backlight_set_native_value_translates_error() -> None:
    """A set_backlight failure becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.number import NumberEntityDescription
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYBacklightNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYBacklightNumberEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=NumberEntityDescription(key=CMD_BACKLIGHT),
        device_info=None,
    )
    assert entity.assumed_state is True
    with (
        patch.object(
            Node, "set_backlight", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Could not set backlight"),
    ):
        await entity.async_set_native_value(50)


async def test_number_description_includes_step_when_editor_has_range() -> None:
    """A control with a resolved editor range gets full
    min/max/step/uom on its NumberEntityDescription (line 92-104)."""
    from unittest.mock import MagicMock, patch

    from custom_components.udi_iox.number import _number_description

    fake_range = MagicMock(min=0, max=100, step=None, precision=2, uom="51")
    isy_data = MagicMock()
    node = MagicMock()
    with (
        patch(
            "custom_components.udi_iox.number.range_for_control",
            return_value=fake_range,
        ),
        patch(
            "custom_components.udi_iox.number.CONTROL_DESC",
            {},  # force the editor-derived path
        ),
    ):
        desc = _number_description(isy_data, node, "OL")
    assert desc.native_min_value == 0.0
    assert desc.native_max_value == 100.0
    # precision=2 → step 10^-2 = 0.01
    assert desc.native_step == 0.01


async def test_async_setup_entry_creates_backlight_entity(hass) -> None:
    """A CMD_BACKLIGHT control in aux_properties[NUMBER] creates an
    ``ISYBacklightNumberEntity`` instead of the generic aux-number
    (lines 166-168)."""
    from unittest.mock import MagicMock

    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import (
        ISYBacklightNumberEntity,
        async_setup_entry,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    isy_data.aux_properties[Platform.NUMBER].append((node, CMD_BACKLIGHT))
    entry = MagicMock()
    entry.runtime_data = isy_data
    entry.options = {}
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert any(isinstance(e, ISYBacklightNumberEntity) for e in collected)


async def test_aux_writeonly_restores_optimistic_value_on_add(hass) -> None:
    """``async_added_to_hass`` for a write-only aux number restores the
    last optimistic value (line 217-218)."""
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYAuxControlNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYAuxControlNumberEntity(
        isy_data=isy_data,
        node=node,
        control="SETME",  # write-only — not in nodedef.properties
        unique_id="x_set",
        description=NumberEntityDescription(key="SETME"),
        device_info=None,
    )
    entity.hass = hass
    entity.entity_id = "number.x"
    last = MagicMock()
    last.native_value = 77.0
    entity.async_get_last_number_data = AsyncMock(return_value=last)
    await entity.async_added_to_hass()
    assert entity._optimistic_value == 77.0


async def test_backlight_async_added_restores_last_value(hass) -> None:
    """The backlight entity restores its last value when both
    ``async_get_last_state`` and ``async_get_last_number_data`` return
    something usable (lines 411-419)."""
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYBacklightNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYBacklightNumberEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=NumberEntityDescription(key=CMD_BACKLIGHT),
        device_info=None,
    )
    entity.hass = hass
    entity.entity_id = "number.bl"
    last_state = MagicMock()
    last_state.state = "55"
    last_data = MagicMock()
    last_data.native_value = 55.0
    entity.async_get_last_state = AsyncMock(return_value=last_state)
    entity.async_get_last_number_data = AsyncMock(return_value=last_data)
    await entity.async_added_to_hass()
    assert entity._attr_native_value == 55.0


async def test_backlight_memory_write_skips_when_value_unchanged() -> None:
    """If the computed % matches the current ``native_value`` the
    handler bails out without firing ``async_write_ha_state``
    (line 442-443)."""
    from unittest.mock import patch

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.const import BACKLIGHT_MEMORY_FILTER
    from custom_components.udi_iox.number import ISYBacklightNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYBacklightNumberEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=NumberEntityDescription(key=CMD_BACKLIGHT),
        device_info=None,
    )
    entity._attr_native_value = 50  # match what value=64 maps to
    from types import SimpleNamespace

    frame = SimpleNamespace(
        memory=BACKLIGHT_MEMORY_FILTER["memory"],
        cmd1=BACKLIGHT_MEMORY_FILTER["cmd1"],
        value=64,
    )
    write_calls = []
    with patch.object(
        ISYBacklightNumberEntity,
        "async_write_ha_state",
        lambda s: write_calls.append(1),
    ):
        entity._on_memory_write(frame)  # type: ignore[arg-type]
    assert write_calls == []


async def test_backlight_set_native_value_success_path() -> None:
    """Successful ``async_set_native_value`` stores the value and
    fires HA state write (lines 457-458)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox import Node
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.number import ISYBacklightNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYBacklightNumberEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=NumberEntityDescription(key=CMD_BACKLIGHT),
        device_info=None,
    )
    write_calls = []
    with (
        patch.object(Node, "set_backlight", new=AsyncMock()),
        patch.object(
            ISYBacklightNumberEntity,
            "async_write_ha_state",
            lambda s: write_calls.append(1),
        ),
    ):
        await entity.async_set_native_value(42)
    assert entity._attr_native_value == 42
    assert write_calls == [1]


async def test_backlight_memory_write_filter() -> None:
    """``_on_memory_write`` updates the native value only when the
    memory address + cmd1 match the backlight filter."""
    from unittest.mock import patch

    from homeassistant.components.number import NumberEntityDescription
    from pyisyox.constants import CMD_BACKLIGHT
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.const import BACKLIGHT_MEMORY_FILTER
    from custom_components.udi_iox.number import ISYBacklightNumberEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYBacklightNumberEntity(
        isy_data=isy_data,
        node=node,
        control=CMD_BACKLIGHT,
        unique_id="x_bl",
        description=NumberEntityDescription(key=CMD_BACKLIGHT),
        device_info=None,
    )

    from types import SimpleNamespace

    def _frame(value: object, cmd1: str | None = None) -> SimpleNamespace:
        """Build a frame stub with the backlight memory address + value."""
        return SimpleNamespace(
            memory=BACKLIGHT_MEMORY_FILTER["memory"],
            cmd1=cmd1 if cmd1 is not None else BACKLIGHT_MEMORY_FILTER["cmd1"],
            value=value,
        )

    with patch.object(ISYBacklightNumberEntity, "async_write_ha_state", lambda s: None):
        entity._on_memory_write(_frame(64))  # type: ignore[arg-type]
        assert entity.native_value == 50  # 64/127 ≈ 50%

        # Same address but wrong cmd1 → ignored.
        entity._on_memory_write(_frame(64, cmd1="DEAD"))  # type: ignore[arg-type]
        assert entity.native_value == 50  # unchanged

        # Missing raw value → ignored.
        entity._on_memory_write(_frame(None))  # type: ignore[arg-type]
        assert entity.native_value == 50
