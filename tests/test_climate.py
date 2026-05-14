"""Snapshot tests for the udi_iox climate platform."""

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
    return [Platform.CLIMATE]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_climate_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every climate entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_set_hvac_mode_translates_node_command_error() -> None:
    """A controller-side rejection on HVAC mode set becomes
    HomeAssistantError (was previously unhandled, bubbling a raw
    NodeCommandError up into the HA event loop)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.climate import HVACMode
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    controller = make_controller(make_load_result())
    node = make_node(
        make_node_record("T 1", "Thermostat", nodedef_id="Thermostat"),
        controller,
    )
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYThermostatEntity(isy_data, node, device_info=None)
    with (
        patch.object(
            Node,
            "set_climate_mode",
            new=AsyncMock(side_effect=NodeCommandError("nope")),
        ),
        pytest.raises(HomeAssistantError, match="Unable to set HVAC mode"),
    ):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
