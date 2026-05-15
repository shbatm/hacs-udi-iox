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

from tests.conftest import isy_data_for


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
    isy_data = isy_data_for(controller)
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
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    with (
        patch.object(
            Node, "nodedef", new_callable=lambda: property(lambda _self: dimmer_def)
        ),
        patch.object(Node, "send_command", new=AsyncMock()) as send_command,
    ):
        await entity.async_turn_on(brightness=138)  # 138/255 → 54 %

    assert send_command.call_args_list[0].args == ("DON", 54)


async def test_is_on_and_brightness_handle_unknown_status() -> None:
    """``is_on`` returns False when status is unknown;
    ``brightness`` returns None."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ISYLightEntity

    controller = make_controller(make_load_result())
    # Empty properties dict → node.status is None → node_status_int → None.
    node = make_node(make_node_record("A 1", "Dimmer", properties={}), controller)
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    assert entity.is_on is False
    assert entity.brightness is None


async def test_brightness_passes_through_non_percentage_uom() -> None:
    """A non-percentage UOM returns the raw status value (no scale)
    ."""
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.entity import node_status_int
    from custom_components.udi_iox.light import ISYLightEntity

    controller = make_controller(make_load_result())
    # UOM "0" — generic, no codec normalization applied.
    record = make_node_record(
        "A 1",
        "Dimmer",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="200", formatted="200", uom="0", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    # Brightness should equal the raw status — no percent→255 scale.
    assert entity.brightness == node_status_int(node)


async def test_async_on_update_records_last_brightness() -> None:
    """``async_on_update`` saves the current brightness so a
    later DON-without-level can restore it.
    Both UOM 51 (percent) and the raw 0-255 path are exercised."""
    from unittest.mock import patch

    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ISYLightEntity

    controller = make_controller(make_load_result())
    # UOM 51 (percent) → status * 255 / 100.
    record = make_node_record(
        "A 1",
        "Dimmer",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="50", formatted="50%", uom="51", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=True)
    with patch.object(ISYLightEntity, "async_write_ha_state", lambda s: None):
        entity.async_on_update(None, "ST")  # type: ignore[arg-type]
    assert entity._last_brightness == round(50 * 255.0 / 100.0)

    # Non-percentage UOM raw → stored as-is.
    record_raw = make_node_record(
        "A 2",
        "Dimmer",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="200", formatted="200", uom="0", name="Status"
            )
        },
    )
    node_raw = make_node(record_raw, controller)
    entity_raw = ISYLightEntity(isy_data, node_raw, restore_light_state=True)
    from custom_components.udi_iox.entity import node_status_int

    expected = node_status_int(node_raw)
    with patch.object(ISYLightEntity, "async_write_ha_state", lambda s: None):
        entity_raw.async_on_update(None, "ST")  # type: ignore[arg-type]
    assert entity_raw._last_brightness == expected


async def test_turn_on_restores_last_brightness_when_enabled() -> None:
    """``restore_light_state=True`` and brightness=None pulls the
    cached ``_last_brightness``."""
    from unittest.mock import AsyncMock, patch

    from pyisyox import Node
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ISYLightEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Dimmer"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=True)
    entity._last_brightness = 128
    with patch.object(Node, "send_command", new=AsyncMock()) as send:
        await entity.async_turn_on()
    # The stored brightness is fed into the standard editor scaling
    # path; the integration sends ``DON`` with the editor-scaled level.
    assert send.await_args.args[0] == "DON"
    assert send.await_args.args[1] in (50, 128)  # scaled-percent or raw


async def test_async_added_to_hass_restores_last_brightness_attr(hass) -> None:
    """``async_added_to_hass`` repopulates ``_last_brightness`` from the
    persisted state attribute when one is present."""
    from unittest.mock import AsyncMock, MagicMock

    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.light import ATTR_LAST_BRIGHTNESS, ISYLightEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Dimmer"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=True)
    entity.hass = hass
    entity.entity_id = "light.dimmer"
    last = MagicMock()
    last.attributes = {ATTR_LAST_BRIGHTNESS: 200}
    entity.async_get_last_state = AsyncMock(return_value=last)
    await entity.async_added_to_hass()
    assert entity._last_brightness == 200


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

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Dimmer"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYLightEntity(isy_data, node, restore_light_state=False)
    with patch.object(
        Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
    ):
        with pytest.raises(HomeAssistantError, match="Unable to turn on light"):
            await entity.async_turn_on()
        with pytest.raises(HomeAssistantError, match="Unable to turn off light"):
            await entity.async_turn_off()
