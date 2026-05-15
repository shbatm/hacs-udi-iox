"""Snapshot tests for the udi_iox switch platform."""

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
    return [Platform.SWITCH]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_switch_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every switch entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_enable_switch_toggles_node_enabled() -> None:
    """The per-device enable switch calls ``Node.set_enabled`` (the v6
    replacement for PyISY 3.x's ``Node.enable()`` / ``disable()``)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.switch import SwitchDeviceClass
    from homeassistant.const import EntityCategory
    from pyisyox.constants import TAG_ENABLED
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import (
        ISYEnableSwitchEntity,
        ISYSwitchEntityDescription,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    description = ISYSwitchEntityDescription(
        key=TAG_ENABLED,
        device_class=SwitchDeviceClass.SWITCH,
        name="Enabled",
        entity_category=EntityCategory.CONFIG,
    )
    entity = ISYEnableSwitchEntity(
        isy_data,
        node=node,
        control=TAG_ENABLED,
        unique_id="x_enabled",
        description=description,
        device_info=None,
    )

    set_enabled = AsyncMock()
    with (
        patch.object(type(node), "set_enabled", set_enabled),
        patch.object(entity, "async_write_ha_state"),
    ):
        await entity.async_turn_off()
        await entity.async_turn_on()

    assert [c.args for c in set_enabled.await_args_list] == [(False,), (True,)]


async def test_enable_switch_always_available_and_tracks_record() -> None:
    """The enable switch must never go unavailable (else there'd be no
    way to switch a disabled node back on), and ``is_on`` follows the
    node record — which pyisyox flips on ``EN`` lifecycle frames."""
    from homeassistant.components.switch import SwitchDeviceClass
    from homeassistant.const import EntityCategory
    from pyisyox.constants import TAG_ENABLED
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import (
        ISYEnableSwitchEntity,
        ISYSwitchEntityDescription,
    )

    controller = make_controller(make_load_result())
    record = make_node_record("AA AA AA 1", "Lamp")
    record.enabled = False  # start disabled
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYEnableSwitchEntity(
        isy_data,
        node=node,
        control=TAG_ENABLED,
        unique_id="x_enabled",
        description=ISYSwitchEntityDescription(
            key=TAG_ENABLED,
            device_class=SwitchDeviceClass.SWITCH,
            name="Enabled",
            entity_category=EntityCategory.CONFIG,
        ),
        device_info=None,
    )

    assert entity.available is True
    assert entity.is_on is False
    record.enabled = True  # e.g. re-enabled from the admin console
    assert entity.available is True
    assert entity.is_on is True


async def test_enable_switch_translates_set_enabled_failure() -> None:
    """A failure on ``Node.set_enabled`` (broad-except: the set-enabled
    path can raise anything from the transport) becomes
    HomeAssistantError with verb-appropriate messaging."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.components.switch import SwitchDeviceClass
    from homeassistant.const import EntityCategory
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox.constants import TAG_ENABLED
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import (
        ISYEnableSwitchEntity,
        ISYSwitchEntityDescription,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYEnableSwitchEntity(
        isy_data,
        node=node,
        control=TAG_ENABLED,
        unique_id="x_enabled",
        description=ISYSwitchEntityDescription(
            key=TAG_ENABLED,
            device_class=SwitchDeviceClass.SWITCH,
            name="Enabled",
            entity_category=EntityCategory.CONFIG,
        ),
        device_info=None,
    )
    with patch.object(
        type(node), "set_enabled", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        with pytest.raises(HomeAssistantError, match="Unable to enable device"):
            await entity.async_turn_on()
        with pytest.raises(HomeAssistantError, match="Unable to disable device"):
            await entity.async_turn_off()


# --- Coverage fillers: switch/group/program entity behaviours ---


async def test_switch_entity_is_on_reflects_status() -> None:
    """ISYSwitchEntity.is_on is True for any non-zero status, False for
    zero, and None when the status is unparsable."""
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import ISYSwitchEntity

    controller = make_controller(make_load_result())
    isy_data = isy_data_for(controller)

    for raw, expected in [("100", True), ("0", False), (None, None)]:
        record = make_node_record(
            "A 1",
            "Lamp",
            properties={
                "ST": NodePropertyValue(
                    id="ST", value=raw, formatted="", uom="100", name="Status"
                )
            },
        )
        node = make_node(record, controller)
        entity = ISYSwitchEntity(isy_data, node=node, device_info=None)
        assert entity.is_on is expected, f"raw={raw}"


async def test_switch_turn_on_off_success_paths() -> None:
    """Successful turn_on / turn_off dispatch the expected wire command."""
    from unittest.mock import AsyncMock, patch

    from pyisyox import Node
    from pyisyox.constants import CMD_OFF, CMD_ON
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import ISYSwitchEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Lamp"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYSwitchEntity(isy_data, node=node, device_info=None)

    send_command = AsyncMock()
    with patch.object(Node, "send_command", new=send_command):
        await entity.async_turn_on()
        await entity.async_turn_off()
    assert [c.args for c in send_command.await_args_list] == [(CMD_ON,), (CMD_OFF,)]


async def test_switch_program_entity_round_trip() -> None:
    """Program switch: is_on reads status program; turn_on / turn_off
    delegate to actions program; both translate failures + success."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_program_record,
    )

    from custom_components.udi_iox.switch import ISYSwitchProgramEntity

    controller = make_controller(make_load_result())
    status = Program(
        make_program_record("0001", "Status", status=True), controller._client
    )
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = isy_data_for(controller)
    entity = ISYSwitchProgramEntity(isy_data, "Scene", status, actions)

    assert entity.is_on is True

    with (
        patch.object(
            Program, "run_then", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to turn on switch program"),
    ):
        await entity.async_turn_on()
    with (
        patch.object(
            Program, "run_else", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to turn off switch program"),
    ):
        await entity.async_turn_off()

    with patch.object(Program, "run_then", new=AsyncMock()):
        await entity.async_turn_on()
    with patch.object(Program, "run_else", new=AsyncMock()):
        await entity.async_turn_off()


# --- Coverage push: error paths, async_on_update, program switches ---


async def test_async_setup_entry_skips_program_switches_without_device_info(
    hass,
) -> None:
    """A program in ``program_devices`` whose DeviceInfo wasn't
    registered is silently skipped (lines 87-88)."""
    from unittest.mock import MagicMock

    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.switch import (
        ISYProgramEnableSwitch,
        ISYProgramRunAtStartupSwitch,
        async_setup_entry,
    )

    controller = make_controller(make_load_result())
    record = make_program_record("0010", "Sunset Lights", path="X")
    isy_data = isy_data_for(controller)
    isy_data.program_devices = [Program(record, controller._client)]
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert not any(
        isinstance(e, (ISYProgramEnableSwitch, ISYProgramRunAtStartupSwitch))
        for e in collected
    )


async def test_switch_turn_off_translates_node_command_error() -> None:
    """A controller-side rejection on turn_off becomes HomeAssistantError
    (lines 132-133)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import ISYSwitchEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYSwitchEntity(isy_data, node=node, device_info=None)
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to turn off switch"),
    ):
        await entity.async_turn_off()


async def test_switch_turn_on_translates_node_command_error() -> None:
    """A controller-side rejection on turn_on becomes HomeAssistantError
    (lines 141-142)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import ISYSwitchEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYSwitchEntity(isy_data, node=node, device_info=None)
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to turn on switch"),
    ):
        await entity.async_turn_on()


async def test_enable_switch_async_on_update_writes_state() -> None:
    """``ISYEnableSwitchEntity.async_on_update`` calls
    ``async_write_ha_state`` (line 243)."""
    from unittest.mock import patch

    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.switch import (
        ISYEnableSwitchEntity,
        ISYSwitchEntityDescription,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Switch"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYEnableSwitchEntity(
        isy_data,
        node=node,
        control="enabled",
        unique_id="x_en",
        description=ISYSwitchEntityDescription(key="enabled", name="Enabled"),
        device_info=None,
    )
    write_calls = []
    with patch.object(
        ISYEnableSwitchEntity,
        "async_write_ha_state",
        lambda s: write_calls.append(1),
    ):
        entity.async_on_update(None, "")  # type: ignore[arg-type]
    assert write_calls == [1]


@pytest.mark.parametrize(
    ("verb", "match"),
    [
        ("async_turn_on", "Unable to enable program"),
        ("async_turn_off", "Unable to disable program"),
    ],
)
async def test_program_enable_switch_translates_errors(verb: str, match: str) -> None:
    """``ISYProgramEnableSwitch`` enable/disable errors surface as
    HomeAssistantError (lines 303-304, 312-313)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.switch import ISYProgramEnableSwitch

    controller = make_controller(make_load_result())
    record = make_program_record("0010", "X", path="X")
    program = Program(record, controller._client)
    isy_data = isy_data_for(controller)
    switch = ISYProgramEnableSwitch(isy_data, program, device_info={})  # type: ignore[arg-type]
    target = "enable" if verb == "async_turn_on" else "disable"
    with (
        patch.object(Program, target, new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(HomeAssistantError, match=match),
    ):
        await getattr(switch, verb)()


@pytest.mark.parametrize(
    ("verb", "match"),
    [
        ("async_turn_on", "Unable to enable run-at-startup"),
        ("async_turn_off", "Unable to disable run-at-startup"),
    ],
)
async def test_program_run_at_startup_switch_translates_errors(
    verb: str, match: str
) -> None:
    """``ISYProgramRunAtStartupSwitch`` enable/disable errors surface as
    HomeAssistantError (lines 342-343, 351-352)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.switch import ISYProgramRunAtStartupSwitch

    controller = make_controller(make_load_result())
    record = make_program_record("0010", "X", path="X")
    program = Program(record, controller._client)
    isy_data = isy_data_for(controller)
    switch = ISYProgramRunAtStartupSwitch(isy_data, program, device_info={})  # type: ignore[arg-type]
    target = (
        "enable_run_at_startup" if verb == "async_turn_on" else "disable_run_at_startup"
    )
    with (
        patch.object(Program, target, new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(HomeAssistantError, match=match),
    ):
        await getattr(switch, verb)()
