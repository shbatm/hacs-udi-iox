"""Support for ISY sensors."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    Platform,
    UnitOfReactivePower,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from pyisyox import Node, NodePropertyValue, Program
from pyisyox.constants import (
    COMMAND_FRIENDLY_NAME,
    PROP_BATTERY_LEVEL,
    PROP_COMMS_ERROR,
    PROP_ENERGY_MODE,
    PROP_HEAT_COOL_STATE,
    PROP_HUMIDITY,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
    PROP_STATUS,
    PROP_TEMPERATURE,
)
from pyisyox.runtime.events import ProgramRunState

from .const import (
    _LOGGER,
    TOTAL_INCREASING_DEVICE_CLASSES,
    UOM_DOUBLE_TEMP,
    UOM_FRIENDLY_NAME,
    UOM_INDEX,
    UOM_ON_OFF,
    UOM_TO_STATES,
    UnitOfApparentPower,
)
from .entity import ISYNodeEntity, _resolve_device_info
from .helpers import convert_isy_value_to_hass
from .models import IsyConfigEntry, IsyData
from .program_device import (
    PROGRAM_LAST_FINISH_SENSOR_SUFFIX,
    PROGRAM_LAST_RUN_SENSOR_SUFFIX,
    PROGRAM_NEXT_SCHEDULED_SENSOR_SUFFIX,
    PROGRAM_RUNNING_SENSOR_SUFFIX,
    ISYProgramDeviceEntity,
)

# Disable general purpose and redundant sensors by default
AUX_DISABLED_BY_DEFAULT_MATCH = ["DO"]
AUX_DISABLED_BY_DEFAULT_EXACT = {
    PROP_COMMS_ERROR,
    PROP_ENERGY_MODE,
    PROP_HEAT_COOL_STATE,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
    PROP_STATUS,
}

PROP_CURRENT_POWER = "CPW"
PROP_TOTAL_POWER = "TPW"


def _check_volume_flow_rate_uom(
    device_class: SensorDeviceClass | None,
    uom: str | list[str] | None,
) -> SensorDeviceClass | None:
    """Check if the volume flow rate unit is supported."""
    if device_class != SensorDeviceClass.VOLUME_FLOW_RATE:
        return device_class
    # Backwards compatibility for ISYv4 firmware which may return a list.
    if isinstance(uom, list):
        uom = uom[0] if uom else None
    if uom is not None and UOM_FRIENDLY_NAME.get(uom) in UnitOfVolumeFlowRate:
        return device_class
    return None


UOM_TO_DEVICE_CLASS = {
    "1": SensorDeviceClass.CURRENT,
    "3": SensorDeviceClass.POWER,
    "4": SensorDeviceClass.TEMPERATURE,
    "7": SensorDeviceClass.VOLUME_FLOW_RATE,
    "12": SensorDeviceClass.SOUND_PRESSURE,
    "13": SensorDeviceClass.SOUND_PRESSURE,
    "17": SensorDeviceClass.TEMPERATURE,
    "23": SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    "24": SensorDeviceClass.PRECIPITATION_INTENSITY,
    "26": SensorDeviceClass.TEMPERATURE,
    "28": SensorDeviceClass.WEIGHT,
    "29": SensorDeviceClass.VOLTAGE,
    "30": SensorDeviceClass.POWER,
    "31": SensorDeviceClass.PRESSURE,
    "32": SensorDeviceClass.SPEED,
    "33": SensorDeviceClass.ENERGY,
    "35": SensorDeviceClass.WATER,
    "39": SensorDeviceClass.VOLUME_FLOW_RATE,
    "40": SensorDeviceClass.SPEED,
    "41": SensorDeviceClass.CURRENT,
    "43": SensorDeviceClass.VOLTAGE,
    "46": SensorDeviceClass.PRECIPITATION_INTENSITY,
    "48": SensorDeviceClass.SPEED,
    "49": SensorDeviceClass.SPEED,
    "52": SensorDeviceClass.WEIGHT,
    "54": SensorDeviceClass.CO2,
    "69": SensorDeviceClass.WATER,
    "72": SensorDeviceClass.VOLTAGE,
    "73": SensorDeviceClass.POWER,
    "74": SensorDeviceClass.IRRADIANCE,
    "82": SensorDeviceClass.DISTANCE,
    "83": SensorDeviceClass.DISTANCE,
    "90": SensorDeviceClass.FREQUENCY,
    "105": SensorDeviceClass.DISTANCE,
    "106": SensorDeviceClass.PRECIPITATION_INTENSITY,
    "116": SensorDeviceClass.DISTANCE,
    "117": SensorDeviceClass.PRESSURE,
    "118": SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    "119": SensorDeviceClass.ENERGY,
    "120": SensorDeviceClass.PRECIPITATION_INTENSITY,
    "127": SensorDeviceClass.PRESSURE,
    "130": SensorDeviceClass.VOLUME_FLOW_RATE,
    "131": SensorDeviceClass.SIGNAL_STRENGTH,
    "133": SensorDeviceClass.FREQUENCY,
    "138": SensorDeviceClass.PRESSURE,
    "142": SensorDeviceClass.VOLUME_FLOW_RATE,
    "143": SensorDeviceClass.VOLUME_FLOW_RATE,
    "144": SensorDeviceClass.VOLUME_FLOW_RATE,
}

# Reference pyisyox.constants.COMMAND_FRIENDLY_NAME for API details.
#   Note: "LUMIN"/Illuminance removed, some devices use non-conformant "%" unit
#         "VOCLVL"/VOC removed, uses qualitative UOM not ug/m^3
ISY_CONTROL_TO_DEVICE_CLASS = {
    PROP_BATTERY_LEVEL: SensorDeviceClass.BATTERY,
    PROP_HUMIDITY: SensorDeviceClass.HUMIDITY,
    PROP_TEMPERATURE: SensorDeviceClass.TEMPERATURE,
    "BARPRES": SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    "CC": SensorDeviceClass.CURRENT,
    "CO2LVL": SensorDeviceClass.CO2,
    PROP_CURRENT_POWER: SensorDeviceClass.POWER,
    "CV": SensorDeviceClass.VOLTAGE,
    "DEWPT": SensorDeviceClass.TEMPERATURE,
    "DISTANC": SensorDeviceClass.DISTANCE,
    "ETO": SensorDeviceClass.PRECIPITATION_INTENSITY,
    "FATM": SensorDeviceClass.WEIGHT,
    "FLOW": SensorDeviceClass.VOLUME_FLOW_RATE,
    "FREQ": SensorDeviceClass.FREQUENCY,
    "MUSCLEM": SensorDeviceClass.WEIGHT,
    "PF": SensorDeviceClass.POWER_FACTOR,
    "PM10": SensorDeviceClass.PM10,
    "PM25": SensorDeviceClass.PM25,
    "PRECIP": SensorDeviceClass.PRECIPITATION,
    "RAINRT": SensorDeviceClass.PRECIPITATION_INTENSITY,
    "RFSS": SensorDeviceClass.SIGNAL_STRENGTH,
    "SOILH": SensorDeviceClass.MOISTURE,
    "SOILT": SensorDeviceClass.TEMPERATURE,
    "SOLRAD": SensorDeviceClass.IRRADIANCE,
    "SPEED": SensorDeviceClass.SPEED,
    "TEMPEXH": SensorDeviceClass.TEMPERATURE,
    "TEMPOUT": SensorDeviceClass.TEMPERATURE,
    PROP_TOTAL_POWER: SensorDeviceClass.ENERGY,
    "WATERP": SensorDeviceClass.PRESSURE,
    "WATERT": SensorDeviceClass.TEMPERATURE,
    "WATERTB": SensorDeviceClass.TEMPERATURE,
    "WATERTD": SensorDeviceClass.TEMPERATURE,
    "WEIGHT": SensorDeviceClass.WEIGHT,
    "WINDCH": SensorDeviceClass.TEMPERATURE,
}
ISY_CONTROL_TO_STATE_CLASS = {
    control: (
        SensorStateClass.MEASUREMENT
        if control != PROP_TOTAL_POWER
        else SensorStateClass.TOTAL_INCREASING
    )
    for control in ISY_CONTROL_TO_DEVICE_CLASS
}
ISY_CONTROL_TO_ENTITY_CATEGORY = {
    PROP_RAMP_RATE: EntityCategory.DIAGNOSTIC,
    PROP_ON_LEVEL: EntityCategory.DIAGNOSTIC,
    PROP_COMMS_ERROR: EntityCategory.DIAGNOSTIC,
}

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the IoX sensor platform."""
    isy_data = entry.runtime_data
    controller = isy_data.root
    entities: list[ISYSensorEntity] = []
    devices: dict[str, DeviceInfo] = isy_data.devices

    entity_list: list[tuple[Node, str]] = [
        *[(node, PROP_STATUS) for node in isy_data.nodes[Platform.SENSOR]],
        *isy_data.aux_properties[Platform.SENSOR],
    ]

    def get_native_uom(
        uom: str | list, node: Node, control: str = PROP_STATUS
    ) -> tuple[str | None, dict[int, str] | None, bool]:
        """Get the native UoM and Options Dict for the ISY sensor device.

        Returns a tuple of the Native UOM, the Options List if it exists,
        and whether or not the UOM is enumerated.
        """
        # Backwards compatibility for ISYv4 Firmware:
        if isinstance(uom, list):
            return (UOM_FRIENDLY_NAME.get(uom[0], uom[0]), None, False)
        # Special cases for ISY UOM index units:
        if isy_states := UOM_TO_STATES.get(uom):
            return (None, isy_states, True)
        if (
            uom == UOM_INDEX
            and (node_def := node.nodedef) is not None
            and (prop := node_def.properties.get(control)) is not None
            and (
                editor := controller.profile.find_editor(
                    prop.editor_id, node.family_id, node.instance_id
                )
            )
            is not None
            and (names := getattr(editor, "names", None)) is not None
        ):
            return (None, names, True)
        # Handle on/off or unlisted index types
        if uom in (UOM_ON_OFF, UOM_INDEX):
            return (None, None, True)
        # Assume double-temp matches current Hass unit (no way to confirm)
        if uom == UOM_DOUBLE_TEMP:
            return (hass.config.units.temperature_unit, None, False)
        return (UOM_FRIENDLY_NAME.get(uom), None, False)

    for node, control in entity_list:
        _LOGGER.debug("Loading %s %s", node.name, COMMAND_FRIENDLY_NAME.get(control))
        enabled_default = control not in AUX_DISABLED_BY_DEFAULT_EXACT and not any(
            control.startswith(match) for match in AUX_DISABLED_BY_DEFAULT_MATCH
        )

        device_class = ISY_CONTROL_TO_DEVICE_CLASS.get(control)
        native_uom = None
        options_dict = None

        if (prop := node.properties.get(control)) is not None:
            # Lookup native units and options list if it has one
            native_uom, options_dict, is_enum = get_native_uom(prop.uom, node, control)

            raw_uom = prop.uom[0] if isinstance(prop.uom, list) else prop.uom

            if device_class is None:
                device_class = UOM_TO_DEVICE_CLASS.get(raw_uom)

            device_class = _check_volume_flow_rate_uom(device_class, prop.uom)

            if is_enum:
                # This is an ISY Enum-type Sensor with an Options List, force Enum Class
                device_class = SensorDeviceClass.ENUM
            elif native_uom is None:
                # Unknown UOMs cause errors with numeric device classes;
                # fall back to the formatted display value instead.
                device_class = None

            # QUIRK: ISY does not differentiate real, apparent, or reactive power:
            if control == PROP_CURRENT_POWER:
                if native_uom == UnitOfApparentPower.VOLT_AMPERE:
                    device_class = SensorDeviceClass.APPARENT_POWER
                elif native_uom == UnitOfReactivePower.VOLT_AMPERE_REACTIVE:
                    device_class = SensorDeviceClass.REACTIVE_POWER

        if device_class == SensorDeviceClass.ENUM:
            state_class = None
        elif device_class in TOTAL_INCREASING_DEVICE_CLASSES:
            state_class = SensorStateClass.TOTAL_INCREASING
        elif device_class is not None:
            state_class = SensorStateClass.MEASUREMENT
        else:
            state_class = None

        description = SensorEntityDescription(
            key=f"{node}_{control}",
            device_class=device_class,
            native_unit_of_measurement=native_uom,
            options=list(options_dict.values()) if options_dict else None,
            state_class=state_class,
            entity_category=ISY_CONTROL_TO_ENTITY_CATEGORY.get(control),
            entity_registry_enabled_default=enabled_default,
        )

        entity = ISYSensorEntity(
            isy_data,
            node=node,
            control=control,
            description=description,
            unique_id=f"{isy_data.uid_base(node)}_{control}"
            if control != PROP_STATUS
            else None,
            device_info=_resolve_device_info(devices, node),
            options_dict=options_dict,
        )
        entities.append(entity)

    for program in isy_data.program_devices:
        device_info = devices.get(f"program_{program.address}")
        if device_info is None:
            continue
        entities.append(ISYProgramRunningSensor(isy_data, program, device_info))
        entities.append(ISYProgramLastRunSensor(isy_data, program, device_info))
        entities.append(ISYProgramLastFinishSensor(isy_data, program, device_info))
        entities.append(ISYProgramNextScheduledSensor(isy_data, program, device_info))

    async_add_entities(entities)


class ISYSensorEntity(ISYNodeEntity, SensorEntity):
    """Representation of an ISY sensor device."""

    _options_dict: dict[int, str] | None
    entity_description: SensorEntityDescription

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        control: str = PROP_STATUS,
        unique_id: str | None = None,
        description: SensorEntityDescription | None = None,
        device_info: DeviceInfo | None = None,
        options_dict: dict[int, str] | None = None,
    ) -> None:
        """Initialize the IoX aux sensor."""
        super().__init__(
            isy_data,
            node=node,
            control=control,
            unique_id=unique_id,
            description=description,
            device_info=device_info,
        )
        self._options_dict = options_dict

    @property
    def target(self) -> NodePropertyValue | None:
        """Return target for the sensor."""
        if self._control not in self._node.properties:
            # Property not yet set (i.e. no errors)
            return None
        return self._node.properties[self._control]

    @property
    def target_value(self) -> Any:
        """Return the target value."""
        return None if self.target is None else self.target.value

    @property
    def native_value(self) -> float | int | str | None:
        """Get the state of the ISY sensor device."""
        if self.target is None or (value := self.target_value) is None:
            return None

        # Check if this is a known index pair UOM
        if self._options_dict is not None:
            return self._options_dict.get(value, value)

        # Check if this is an on/off or unlisted index type and get formatted value
        if self.native_unit_of_measurement is None and self.target.formatted:
            return self.target.formatted

        # Handle ISY precision and rounding
        value = convert_isy_value_to_hass(value, self.target.uom, self.target.precision)

        if value is None:
            return None

        assert isinstance(value, int | float)
        return value


def _parse_iox_timestamp(raw: str | None):
    """Parse an IoX ISO 8601 timestamp into a tz-aware ``datetime``.

    pyisyox surfaces timestamps as the wire string (``"2026-05-10T..."``)
    rather than parsing eagerly; we do it here so timestamp sensors
    expose a real ``datetime``. Returns ``None`` on missing or
    unparsable input — the wire payload omits these fields when the
    program has never run / has no schedule.
    """
    if not raw:
        return None
    try:
        parsed = dt_util.parse_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


_PROGRAM_RUN_STATE_LABELS: dict[ProgramRunState, str] = {
    ProgramRunState.IDLE: "idle",
    ProgramRunState.THEN: "running_then",
    ProgramRunState.ELSE: "running_else",
}


class ISYProgramRunningSensor(ISYProgramDeviceEntity, SensorEntity):
    """Decoded run-state of the program — ``idle`` / ``running_then``
    / ``running_else``, or ``None`` (rendered as ``unknown``) when the
    program is in the cookbook ``ST_NOT_LOADED`` (errored) state."""

    _attr_translation_key = "program_running"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(_PROGRAM_RUN_STATE_LABELS.values())
    _attr_icon = "mdi:run"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        """Initialize the running-state sensor."""
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_RUNNING_SENSOR_SUFFIX
        )

    @property
    def native_value(self) -> str | None:
        """Map pyisyox's typed :class:`ProgramRunState` to an HA enum option."""
        run_state = self._node.run_state
        if run_state is None:
            return None
        return _PROGRAM_RUN_STATE_LABELS.get(run_state)


class _ISYProgramTimestampSensor(ISYProgramDeviceEntity, SensorEntity):
    """Shared logic for the three timestamp sensors on a program device."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False

    # Subclasses must override; declared without a default so a missing
    # override surfaces immediately as a mypy / runtime AttributeError
    # rather than silently calling ``getattr(node, "")``.
    _source_attr: ClassVar[str]

    @property
    def native_value(self):
        """Parsed timestamp value off the program's record."""
        return _parse_iox_timestamp(getattr(self._node, self._source_attr))


class ISYProgramLastRunSensor(_ISYProgramTimestampSensor):
    """``Program.last_run_time`` as a timestamp sensor."""

    _attr_translation_key = "program_last_run"
    _source_attr = "last_run_time"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_LAST_RUN_SENSOR_SUFFIX
        )


class ISYProgramLastFinishSensor(_ISYProgramTimestampSensor):
    """``Program.last_finish_time`` as a timestamp sensor."""

    _attr_translation_key = "program_last_finish"
    _source_attr = "last_finish_time"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_LAST_FINISH_SENSOR_SUFFIX
        )


class ISYProgramNextScheduledSensor(_ISYProgramTimestampSensor):
    """``Program.next_scheduled_run_time`` as a timestamp sensor."""

    _attr_translation_key = "program_next_scheduled"
    _source_attr = "next_scheduled_run_time"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_NEXT_SCHEDULED_SENSOR_SUFFIX
        )
