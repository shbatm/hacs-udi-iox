"""Snapshot tests for the udi_iox fan platform."""

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
    return [Platform.FAN]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_fan_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every fan entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_fanlinc_speed_driven_by_st_editor() -> None:
    """``FanLincMotor``'s ``ST`` editor (``I_FLM_LVL``) enumerates
    ``0/25/75/100`` ⇒ Off/Low/Medium/High. The fan exposes 3 ordered
    speeds, reads back the percentage matching the reported step, and a
    set command is sent as one of the on-list values (never an off-list
    value the controller's editor would reject) via ``DON``."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.util.percentage import ordered_list_item_to_percentage
    from pyisyox.client import NodePropertyValue
    from pyisyox.constants import CMD_OFF, CMD_ON
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.fan import ISYFanEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "EE EE EE 2",
        "FanLinc Motor",
        nodedef_id="FanLincMotor",
        type_="1.46.0.0",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="75", formatted="Medium", uom="51", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYFanEntity(isy_data, node=node, device_info=None)

    assert entity.speed_count == 3
    entity._update_fan_attrs()  # no hass to drive it
    assert entity.is_on is True
    assert entity.percentage == ordered_list_item_to_percentage([25, 75, 100], 75)

    send = AsyncMock()
    with patch.object(type(node), "send_command", send):
        await entity.async_set_percentage(100)  # High
        await entity.async_set_percentage(1)  # smallest non-zero -> Low (25)
        await entity.async_set_percentage(0)  # Off

    assert [c.args for c in send.await_args_list] == [
        (CMD_ON, 100),
        (CMD_ON, 25),
        (CMD_OFF,),
    ]


async def test_set_percentage_translates_node_command_error() -> None:
    """A controller-side rejection on speed-set becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NodeCommandError
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.fan import ISYFanEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "EE EE EE 2",
        "FanLinc Motor",
        nodedef_id="FanLincMotor",
        type_="1.46.0.0",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="0", formatted="Off", uom="51", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYFanEntity(isy_data, node=node, device_info=None)
    with patch.object(
        type(node),
        "send_command",
        new=AsyncMock(side_effect=NodeCommandError("nope")),
    ):
        with pytest.raises(HomeAssistantError, match="Unable to set fan speed"):
            await entity.async_set_percentage(50)
        with pytest.raises(HomeAssistantError, match="Unable to turn off fan"):
            await entity.async_turn_off()


async def test_continuous_fan_uses_ranged_value_when_no_subset() -> None:
    """A fan node with an editor that exposes ``max`` (no enumerated
    speeds) uses the ranged-value path: percentage→raw via the editor's
    ``(1, max)`` range. Drives lines 103-104, 117, 147."""
    from unittest.mock import AsyncMock, patch

    from pyisyox.client import NodePropertyValue
    from pyisyox.constants import CMD_ON
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.fan import ISYFanEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "EE EE EE 2",
        "Continuous Fan",
        nodedef_id="DimmerLampOnly",  # editor I_OL: 0-100, no subset/names
        type_="1.46.0.0",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="50", formatted="50%", uom="51", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYFanEntity(isy_data, node=node, device_info=None)

    # Confirm the continuous-range branch was taken (no ordered_speeds).
    assert entity._ordered_speeds is None
    # ranged_value_to_percentage((1, 100), 50) = ~49 — close enough to 50.
    entity._update_fan_attrs()
    assert entity.is_on is True
    assert entity.percentage is not None and 40 <= entity.percentage <= 60

    send = AsyncMock()
    with patch.object(type(node), "send_command", send):
        await entity.async_set_percentage(75)
    # The ranged path math.ceils — should be ~75 (within 1).
    sent_value = send.await_args.args[1]
    assert send.await_args.args[0] == CMD_ON
    assert 73 <= sent_value <= 77


async def test_fan_attrs_set_to_none_when_status_unknown() -> None:
    """``_update_fan_attrs`` clears ``is_on`` / ``percentage`` to ``None``
    when the underlying status is None (lines 127-128)."""
    from pyisyox.client import NodePropertyValue
    from pyisyox.constants import ISY_VALUE_UNKNOWN
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.fan import ISYFanEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "EE EE EE 2",
        "FanLinc Motor",
        nodedef_id="FanLincMotor",
        type_="1.46.0.0",
        properties={
            "ST": NodePropertyValue(
                id="ST",
                value=ISY_VALUE_UNKNOWN,
                formatted="?",
                uom="51",
                name="Status",
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYFanEntity(isy_data, node=node, device_info=None)
    entity._update_fan_attrs()
    # FanEntity.is_on is computed (not always _attr_is_on); inspect the
    # raw attrs that ``_update_fan_attrs`` set.
    assert entity._attr_is_on is None
    assert entity._attr_percentage is None


async def test_async_on_update_refreshes_attrs_and_dispatches() -> None:
    """``async_on_update`` recomputes the cached attrs and chains to
    the parent (lines 136-137)."""
    from unittest.mock import patch

    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.fan import ISYFanEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "EE EE EE 2",
        "FanLinc Motor",
        nodedef_id="FanLincMotor",
        type_="1.46.0.0",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="0", formatted="Off", uom="51", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYFanEntity(isy_data, node=node, device_info=None)
    with patch.object(ISYFanEntity, "async_write_ha_state", lambda s: None):
        entity.async_on_update(None, "ST")  # type: ignore[arg-type]
    assert entity.is_on is False


async def test_turn_on_uses_default_percentage_when_none_provided() -> None:
    """``async_turn_on(percentage=None)`` falls through to ``67`` (line 162)."""
    from unittest.mock import AsyncMock, patch

    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.fan import ISYFanEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "EE EE EE 2",
        "FanLinc Motor",
        nodedef_id="FanLincMotor",
        type_="1.46.0.0",
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYFanEntity(isy_data, node=node, device_info=None)
    with patch.object(type(node), "send_command", new=AsyncMock()) as send:
        await entity.async_turn_on()
    # 67% rounds up across the (25/75/100) ordered speeds — exact bucket
    # depends on percentage_to_ordered_list_item's tie-break rule. The
    # important thing is we got an on-list value, not the off-list 67.
    assert send.await_args.args[1] in (75, 100)


async def test_fan_program_entity_is_on_reads_status_program() -> None:
    """``ISYFanProgramEntity.is_on`` returns the status program's bool
    (line 187)."""
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.fan import ISYFanProgramEntity

    controller = make_controller(make_load_result())
    status = Program(
        make_program_record("0001", "Status", status=True), controller._client
    )
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = isy_data_for(controller)
    entity = ISYFanProgramEntity(isy_data, "Fan", status, actions)
    assert entity.is_on is True


async def test_async_setup_entry_creates_program_fan_entities(hass) -> None:
    """A program in ``isy_data.programs[FAN]`` flows into an
    ``ISYFanProgramEntity`` (line 60-61)."""
    from unittest.mock import MagicMock

    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.fan import ISYFanProgramEntity, async_setup_entry

    controller = make_controller(make_load_result())
    status = Program(make_program_record("0001", "Status"), controller._client)
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = isy_data_for(controller)
    isy_data.programs[Platform.FAN] = [("Fan Prog", status, actions)]
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert len(collected) == 1
    assert isinstance(collected[0], ISYFanProgramEntity)


async def test_fan_program_translates_run_failure() -> None:
    """A failure in the actions program's run_then/run_else becomes
    HomeAssistantError (broad-except: the program-run surface can
    raise more than NodeCommandError)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.fan import ISYFanProgramEntity

    controller = make_controller(make_load_result())
    status = Program(make_program_record("0001", "Status"), controller._client)
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = isy_data_for(controller)
    entity = ISYFanProgramEntity(isy_data, "Fan", status, actions)
    with (
        patch.object(
            Program, "run_then", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to turn on fan program"),
    ):
        await entity.async_turn_on()
    with (
        patch.object(
            Program, "run_else", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to turn off fan program"),
    ):
        await entity.async_turn_off()
