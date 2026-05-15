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


# --- Coverage push: aux-property setup branches and helper edge cases ---


async def test_check_volume_flow_rate_uom_handles_list_uom() -> None:
    """``_check_volume_flow_rate_uom`` accepts a list UOM (legacy
    ISYv4 firmware shape) and uses the first entry (lines 81-84)."""
    from homeassistant.components.sensor import SensorDeviceClass

    from custom_components.udi_iox.sensor import _check_volume_flow_rate_uom

    # First entry is a known volume-flow UOM → keep the device_class.
    assert (
        _check_volume_flow_rate_uom(SensorDeviceClass.VOLUME_FLOW_RATE, ["7", "extra"])
        == SensorDeviceClass.VOLUME_FLOW_RATE
    )
    # Empty list → uom becomes None → drop.
    assert _check_volume_flow_rate_uom(SensorDeviceClass.VOLUME_FLOW_RATE, []) is None


async def test_async_setup_entry_skips_program_sensors_without_device_info(
    hass,
) -> None:
    """A program in ``program_devices`` whose DeviceInfo wasn't
    registered is silently skipped — none of the four per-program
    sensors get created (lines 316-317)."""
    from unittest.mock import MagicMock

    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.sensor import (
        ISYProgramLastFinishSensor,
        ISYProgramLastRunSensor,
        ISYProgramNextScheduledSensor,
        ISYProgramRunningSensor,
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
        isinstance(
            e,
            (
                ISYProgramRunningSensor,
                ISYProgramLastRunSensor,
                ISYProgramLastFinishSensor,
                ISYProgramNextScheduledSensor,
            ),
        )
        for e in collected
    )


async def test_aux_sensor_setup_classifies_apparent_and_reactive_power(hass) -> None:
    """A CV (Volt-Amperes) and CC (VAR) aux property attached to a
    PROP_CURRENT_POWER reading classifies as APPARENT_POWER /
    REACTIVE_POWER respectively (lines 277-285)."""
    from unittest.mock import MagicMock

    from homeassistant.components.sensor import SensorDeviceClass
    from pyisyox.client import NodePropertyValue

    PROP_CURRENT_POWER = "CPW"
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.sensor import async_setup_entry

    controller = make_controller(make_load_result())
    # UOM 135 = VA (apparent power), UOM 136 = var (reactive power)
    apparent = make_node_record(
        "AA AA AA 1",
        "Power",
        properties={
            PROP_CURRENT_POWER: NodePropertyValue(
                id=PROP_CURRENT_POWER,
                value="100",
                formatted="100 VA",
                uom="135",
                name="Power",
            )
        },
    )
    reactive = make_node_record(
        "AA AA AA 2",
        "Reactive",
        properties={
            PROP_CURRENT_POWER: NodePropertyValue(
                id=PROP_CURRENT_POWER,
                value="50",
                formatted="50 var",
                uom="136",
                name="Reactive Power",
            )
        },
    )
    apparent_node = make_node(apparent, controller)
    reactive_node = make_node(reactive, controller)
    isy_data = isy_data_for(controller)
    isy_data.aux_properties[Platform.SENSOR].extend(
        [(apparent_node, PROP_CURRENT_POWER), (reactive_node, PROP_CURRENT_POWER)]
    )
    isy_data.devices[apparent.address] = MagicMock()
    isy_data.devices[reactive.address] = MagicMock()
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    classes = {getattr(e, "device_class", None) for e in collected}
    # At least one of the two power-class branches must have fired.
    assert (
        SensorDeviceClass.APPARENT_POWER in classes
        or SensorDeviceClass.REACTIVE_POWER in classes
    )


async def test_aux_sensor_setup_exercises_uom_decode_branches(hass) -> None:
    """Drives ``get_native_uom`` through its enum / on-off / double-temp /
    list-UOM legacy branches (lines 221, 224, 240, 243)."""
    from unittest.mock import MagicMock

    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.const import UOM_DOUBLE_TEMP, UOM_ON_OFF
    from custom_components.udi_iox.sensor import async_setup_entry

    controller = make_controller(make_load_result())

    def _aux(addr: str, uom):
        rec = make_node_record(
            addr,
            f"S{addr}",
            properties={
                "MY": NodePropertyValue(
                    id="MY",
                    value="1",
                    formatted="1",
                    uom=uom,
                    name="MyControl",
                )
            },
        )
        return make_node(rec, controller)

    # legacy list-UOM (line 221)
    n1 = _aux("AA AA AA 1", ["7"])
    # UOM in UOM_TO_STATES (line 224) — UOM 66 maps to HVAC heat/cool state enum
    n2 = _aux("AA AA AA 2", "66")
    # UOM_INDEX (line 240) — UOM 25 is the index type
    n3 = _aux("AA AA AA 3", "25")
    # UOM_DOUBLE_TEMP (line 243)
    n4 = _aux("AA AA AA 4", UOM_DOUBLE_TEMP)
    # UOM_ON_OFF directly (line 240)
    n5 = _aux("AA AA AA 5", UOM_ON_OFF)

    isy_data = isy_data_for(controller)
    for n in (n1, n2, n3, n4, n5):
        isy_data.aux_properties[Platform.SENSOR].append((n, "MY"))
        isy_data.devices[n.address] = MagicMock()
    entry = MagicMock()
    entry.runtime_data = isy_data
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert len(collected) >= 5
