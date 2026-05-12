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

    from custom_components.udi_iox.models import IsyData
    from custom_components.udi_iox.switch import (
        ISYEnableSwitchEntity,
        ISYSwitchEntityDescription,
    )
    from tests.builders import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = IsyData()
    isy_data.root = controller
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
