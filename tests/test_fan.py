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
    from pyisyox import NodePropertyValue
    from pyisyox.constants import CMD_OFF, CMD_ON

    from custom_components.udi_iox.fan import ISYFanEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

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
    isy_data = IsyData()
    isy_data.root = controller
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
    from pyisyox import NodeCommandError, NodePropertyValue

    from custom_components.udi_iox.fan import ISYFanEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

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
    isy_data = IsyData()
    isy_data.root = controller
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


async def test_fan_program_translates_run_failure() -> None:
    """A failure in the actions program's run_then/run_else becomes
    HomeAssistantError (broad-except: the program-run surface can
    raise more than NodeCommandError)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program

    from custom_components.udi_iox.fan import ISYFanProgramEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import make_controller, make_load_result, make_program_record

    controller = make_controller(make_load_result())
    status = Program(make_program_record("0001", "Status"), controller._client)
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = IsyData()
    isy_data.root = controller
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
