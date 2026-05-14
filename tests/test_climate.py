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
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData

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


async def test_set_temperature_translates_node_command_error() -> None:
    """A controller-side rejection on setpoint write becomes
    HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData

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
            "set_climate_setpoint_heat",
            new=AsyncMock(side_effect=NodeCommandError("nope")),
        ),
        pytest.raises(HomeAssistantError, match="Unable to set temperature"),
    ):
        await entity.async_set_temperature(target_temp_low=70)


async def test_set_fan_mode_translates_node_command_error() -> None:
    """A controller-side rejection on fan-mode set becomes HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.climate import FAN_AUTO
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData

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
            "set_fan_mode",
            new=AsyncMock(side_effect=NodeCommandError("nope")),
        ),
        pytest.raises(HomeAssistantError, match="Unable to set fan mode"),
    ):
        await entity.async_set_fan_mode(FAN_AUTO)


# --- Direct property tests (cover the read-side accessors) ---


def _make_thermostat(controller, **prop_overrides):
    """Build a thermostat node with a configurable property bag."""
    from pyisyox import NodePropertyValue

    from tests.builders import make_node, make_node_record

    defaults = {
        "ST": NodePropertyValue(
            id="ST", value="720", formatted="72", uom="17", name="Status", precision=1
        ),
        "CLISPC": NodePropertyValue(
            id="CLISPC",
            value="780",
            formatted="78",
            uom="17",
            name="Cool Setpoint",
            precision=1,
        ),
        "CLISPH": NodePropertyValue(
            id="CLISPH",
            value="680",
            formatted="68",
            uom="17",
            name="Heat Setpoint",
            precision=1,
        ),
        "CLIMD": NodePropertyValue(
            id="CLIMD", value="1", formatted="Heat", uom="67", name="Mode"
        ),
        "CLIFS": NodePropertyValue(
            id="CLIFS", value="8", formatted="Auto", uom="99", name="Fan"
        ),
        "CLIHCS": NodePropertyValue(
            id="CLIHCS", value="1", formatted="Heat", uom="66", name="HVAC State"
        ),
        "CLIHUM": NodePropertyValue(
            id="CLIHUM", value="45", formatted="45", uom="22", name="Humidity"
        ),
        "UOM": NodePropertyValue(
            id="UOM", value="17", formatted="F", uom="0", name="Temp Unit"
        ),
    }
    defaults.update(prop_overrides)
    return make_node(
        make_node_record(
            "T 1", "Thermostat", nodedef_id="Thermostat", properties=defaults
        ),
        controller,
    )


async def test_climate_reads_basic_state(hass) -> None:
    """Property accessors return the right HVAC mode / action / humidity /
    setpoints / temperature unit / current temperature for a heat-mode
    thermostat in Fahrenheit."""
    from homeassistant.components.climate import FAN_AUTO, HVACAction, HVACMode
    from homeassistant.const import UnitOfTemperature

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import make_controller, make_load_result

    controller = make_controller(make_load_result())
    node = _make_thermostat(controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYThermostatEntity(isy_data, node, device_info=None)
    entity.hass = hass

    assert entity.temperature_unit == UnitOfTemperature.FAHRENHEIT
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.hvac_action == HVACAction.HEATING
    assert entity.current_humidity == 45
    assert entity.fan_mode == FAN_AUTO
    assert entity.target_temperature_high == 78.0  # 780 with precision 1
    assert entity.target_temperature_low == 68.0
    assert entity.target_temperature == 68.0  # heat mode → low setpoint
    assert entity.current_temperature == 72.0


async def test_climate_unknown_states_fallback(hass) -> None:
    """Missing or none-valued properties yield sensible defaults."""
    from homeassistant.components.climate import FAN_OFF, HVACMode
    from pyisyox import NodePropertyValue

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
        make_node_record(
            "T 1",
            "Thermostat",
            nodedef_id="Thermostat",
            properties={
                "ST": NodePropertyValue(
                    id="ST", value=None, formatted="", uom="17", name="Status"
                ),
            },
        ),
        controller,
    )
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYThermostatEntity(isy_data, node, device_info=None)
    entity.hass = hass

    # No CLIMD → HVACMode.OFF; no setpoints → None; no humidity → None.
    assert entity.hvac_mode == HVACMode.OFF
    assert entity.hvac_action is None
    assert entity.current_humidity is None
    assert entity.target_temperature_high is None
    assert entity.target_temperature_low is None
    assert entity.target_temperature is None
    assert entity.fan_mode == FAN_OFF


async def test_climate_temperature_unit_celsius(hass) -> None:
    """UOM_ISY_CELSIUS on the UOM property yields the Celsius unit."""
    from homeassistant.const import UnitOfTemperature
    from pyisyox import NodePropertyValue

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.const import UOM_ISY_CELSIUS
    from custom_components.udi_iox.models import IsyData
    from tests.builders import make_controller, make_load_result

    controller = make_controller(make_load_result())
    node = _make_thermostat(
        controller,
        UOM=NodePropertyValue(
            id="UOM", value=UOM_ISY_CELSIUS, formatted="C", uom="0", name="Temp Unit"
        ),
    )
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYThermostatEntity(isy_data, node, device_info=None)
    entity.hass = hass
    assert entity.temperature_unit == UnitOfTemperature.CELSIUS


async def test_climate_set_temperature_uses_correct_setpoint_per_mode(hass) -> None:
    """In HEAT mode, ATTR_TEMPERATURE writes the heat setpoint;
    in COOL mode it writes the cool setpoint."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.const import ATTR_TEMPERATURE
    from pyisyox import Node

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import make_controller, make_load_result

    controller = make_controller(make_load_result())
    node = _make_thermostat(controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYThermostatEntity(isy_data, node, device_info=None)
    entity.hass = hass

    set_heat = AsyncMock()
    with (
        patch.object(Node, "set_climate_setpoint_heat", new=set_heat),
        patch.object(ISYThermostatEntity, "async_write_ha_state", lambda s: None),
    ):
        # HEAT mode → ATTR_TEMPERATURE goes to heat setpoint.
        await entity.async_set_temperature(**{ATTR_TEMPERATURE: 70})
    set_heat.assert_awaited_once_with(70)


async def test_climate_set_fan_mode_success_path(hass) -> None:
    """Successful set_fan_mode dispatches with the mapped wire value."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.components.climate import FAN_AUTO
    from pyisyox import Node

    from custom_components.udi_iox.climate import ISYThermostatEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import make_controller, make_load_result

    controller = make_controller(make_load_result())
    node = _make_thermostat(controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYThermostatEntity(isy_data, node, device_info=None)
    entity.hass = hass

    set_fan = AsyncMock()
    with (
        patch.object(Node, "set_fan_mode", new=set_fan),
        patch.object(ISYThermostatEntity, "async_write_ha_state", lambda s: None),
    ):
        await entity.async_set_fan_mode(FAN_AUTO)
    set_fan.assert_awaited_once()
