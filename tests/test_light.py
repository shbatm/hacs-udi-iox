"""Snapshot tests for the udi_iox light platform."""

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
    return [Platform.LIGHT]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_light_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every light entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_turn_on_sets_brightness_via_don_level_parameter() -> None:
    """Brightness is set with the ``DON`` command's level parameter (not
    a separate ``OL`` write), scaling HA's 0-255 down to 0-100 when the
    parameter's editor is percent / byte-capped."""
    from unittest.mock import AsyncMock, patch

    from pyisyox import Node
    from pyisyox.schema.cmd import Command, CommandParameter
    from pyisyox.schema.nodedef import NodeCommands, NodeDef
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ISYLightEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(
        make_node_record("A 1", "Dimmer", family_id="1", nodedef_id="DimmerLampOnly"),
        controller,
    )
    # ``DON`` takes an optional level whose editor is ``I_OL`` — uom 51
    # (0-100 %), present in the bundled Insteon profile — so 128/255 → 50.
    dimmer_def = NodeDef(
        id="DimmerLampOnly",
        family_id="1",
        instance_id="1",
        cmds=NodeCommands(
            accepts=[
                Command(
                    id="DON",
                    parameters=[CommandParameter(editor_id="I_OL", optional=True)],
                ),
                Command(id="DOF"),
            ]
        ),
    )
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    with (
        patch.object(
            Node, "nodedef", new_callable=lambda: property(lambda _self: dimmer_def)
        ),
        patch.object(Node, "send_command", new=AsyncMock()) as send_command,
    ):
        await entity.async_turn_on(brightness=128)  # 128/255 → 50 %
        await entity.async_turn_on()  # no brightness ⇒ plain DON

    assert send_command.call_args_list[0].args == ("DON", 50)
    assert send_command.call_args_list[1].args == ("DON",)
    # Never an ``OL`` write — that's a separate device setting.
    assert all(call.args[0] == "DON" for call in send_command.call_args_list)


async def test_turn_on_scales_brightness_for_multirange_zwave_editor() -> None:
    """Multi-range editors (``ZW_DIM_PERCENT``: a tiny ``{1: "Previous Value"}``
    index range alongside a 0-100 % range) must still scale 0-255 → 0-100.

    Regression: ``ranges[0]`` is the index range with no numeric ``max``,
    so the old logic short-circuited and sent 138 raw — which the codec
    then rejected against the percent range's ``max=100``.
    """
    from unittest.mock import AsyncMock, patch

    from pyisyox import Node
    from pyisyox.schema.cmd import Command, CommandParameter
    from pyisyox.schema.nodedef import NodeCommands, NodeDef
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ISYLightEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(
        make_node_record("Z 1", "ZWaveDimmer", family_id="4", nodedef_id="UZW000E"),
        controller,
    )
    # ``ZW_DIM_PERCENT`` ships in the bundled profile (family "4", instance
    # "1") with ranges = [{uom:25, names:{1:"Previous Value"}}, {uom:51,
    # min:0, max:100}]. The first range has no numeric bounds, so the
    # scaling code must walk past it to the percent range.
    dimmer_def = NodeDef(
        id="UZW000E",
        family_id="4",
        instance_id="1",
        cmds=NodeCommands(
            accepts=[
                Command(
                    id="DON",
                    parameters=[
                        CommandParameter(editor_id="ZW_DIM_PERCENT", optional=True)
                    ],
                ),
                Command(id="DOF"),
            ]
        ),
    )
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    with (
        patch.object(
            Node, "nodedef", new_callable=lambda: property(lambda _self: dimmer_def)
        ),
        patch.object(Node, "send_command", new=AsyncMock()) as send_command,
    ):
        await entity.async_turn_on(brightness=138)  # 138/255 → 54 %

    assert send_command.call_args_list[0].args == ("DON", 54)


async def test_turn_on_translates_node_command_error_to_homeassistanterror() -> None:
    """A controller-side rejection (NodeCommandError) becomes a
    HomeAssistantError so HA shows the user a clear failure popup
    instead of silently no-op'ing (was previously logged at DEBUG)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ISYLightEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Dimmer"), controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    with patch.object(
        Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
    ):
        with pytest.raises(HomeAssistantError, match="Unable to turn on light"):
            await entity.async_turn_on()
        with pytest.raises(HomeAssistantError, match="Unable to turn off light"):
            await entity.async_turn_off()
