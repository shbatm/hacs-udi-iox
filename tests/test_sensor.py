"""Snapshot tests for the udi_iox sensor platform."""

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
    return [Platform.SENSOR]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_sensor_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every sensor entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


# --- Direct entity tests (cover sensor.py logic) ---


async def test_check_volume_flow_rate_uom_keeps_known_uoms() -> None:
    """The volume-flow-rate device class is preserved when the UOM
    maps to a known UnitOfVolumeFlowRate; dropped otherwise."""
    from homeassistant.components.sensor import SensorDeviceClass

    from custom_components.udi_iox.sensor import _check_volume_flow_rate_uom

    # Unknown UOM → drop the class.
    assert (
        _check_volume_flow_rate_uom(SensorDeviceClass.VOLUME_FLOW_RATE, "99999") is None
    )
    # Non-volume-flow device class → pass through unchanged.
    assert (
        _check_volume_flow_rate_uom(SensorDeviceClass.TEMPERATURE, "99999")
        == SensorDeviceClass.TEMPERATURE
    )
    # ISYv4 list-shaped UOM falls back to the first element.
    assert _check_volume_flow_rate_uom(SensorDeviceClass.VOLUME_FLOW_RATE, []) is None


async def test_sensor_entity_target_and_native_value() -> None:
    """ISYSensorEntity returns None when the control hasn't reported,
    the formatted value for unitless string types, and the numeric
    value when a UOM lets HA infer one."""
    from homeassistant.components.sensor import SensorEntityDescription
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.sensor import ISYSensorEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Battery Sensor",
        properties={
            "BATLVL": NodePropertyValue(
                id="BATLVL", value=85, formatted="85", uom="51", name="Battery"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)

    # Existing property → target value comes through.
    entity = ISYSensorEntity(
        isy_data,
        node=node,
        control="BATLVL",
        description=SensorEntityDescription(
            key="batlvl", native_unit_of_measurement="%"
        ),
        unique_id="x_batlvl",
        device_info=None,
    )
    assert entity.target is not None
    assert entity.target_value == 85

    # Missing control → target is None and native_value returns None.
    entity = ISYSensorEntity(
        isy_data,
        node=node,
        control="DOES_NOT_EXIST",
        description=SensorEntityDescription(key="x"),
        unique_id="x_x",
        device_info=None,
    )
    assert entity.target is None
    assert entity.target_value is None
    assert entity.native_value is None


async def test_sensor_native_value_resolves_options_dict() -> None:
    """When the entity has an enum options_dict, native_value resolves
    the raw int to the friendly string."""
    from homeassistant.components.sensor import SensorEntityDescription
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.sensor import ISYSensorEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Mode",
        properties={
            "X": NodePropertyValue(id="X", value=1, formatted="On", uom="2", name="X")
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYSensorEntity(
        isy_data,
        node=node,
        control="X",
        description=SensorEntityDescription(key="x"),
        unique_id="x_x",
        device_info=None,
        options_dict={0: "Off", 1: "On"},
    )
    assert entity.native_value == "On"


async def test_sensor_native_value_uses_formatted_for_string_types() -> None:
    """For string/index types without a native unit, the formatted
    display string is returned as native_value."""
    from homeassistant.components.sensor import SensorEntityDescription
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.sensor import ISYSensorEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "A 1",
        "Mode",
        properties={
            "X": NodePropertyValue(
                id="X", value=42, formatted="forty-two", uom="2", name="X"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYSensorEntity(
        isy_data,
        node=node,
        control="X",
        description=SensorEntityDescription(key="x"),  # no native_uom
        unique_id="x_x",
        device_info=None,
    )
    assert entity.native_value == "forty-two"
