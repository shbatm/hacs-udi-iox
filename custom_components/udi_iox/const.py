"""Constants for the ISY Platform."""

import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.climate import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_MEDIUM,
    FAN_ON,
    PRESET_AWAY,
    PRESET_BOOST,
    HVACAction,
    HVACMode,
)
from homeassistant.components.lock import LockState
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    CURRENCY_CENT,
    CURRENCY_DOLLAR,
    DEGREE,
    LIGHT_LUX,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SERVICE_LOCK,
    SERVICE_UNLOCK,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OFF,
    STATE_ON,
    STATE_OPEN,
    STATE_OPENING,
    STATE_PROBLEM,
    STATE_UNKNOWN,
    UV_INDEX,
    Platform,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfMass,
    UnitOfPower,
    UnitOfPressure,
    UnitOfReactivePower,
    UnitOfSoundPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
    UnitOfVolumetricFlux,
)
from pyisyox.constants import (
    DEV_BL_ADDR,
    DEV_CMD_MEMORY_WRITE,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
)

_LOGGER = logging.getLogger(__package__)

DOMAIN = "udi_iox"

MANUFACTURER = "Universal Devices, Inc"

CONF_ENABLE_NETWORKING = "enable_networking"
CONF_ENABLE_PROGRAMS = "enable_programs"
CONF_ENABLE_VARIABLES = "enable_variables"
CONF_IGNORE_STRING = "ignore_string"
CONF_NETWORK = "network"
CONF_RESTORE_LIGHT_STATE = "restore_light_state"
CONF_SENSOR_STRING = "sensor_string"

DEFAULT_IGNORE_STRING = "{IGNORE ME}"
# Bracketed marker (not the bare word "sensor"): the string is matched
# verbatim AND stripped from device/entity names, so a plain word would
# both over-match (node-server entities legitimately containing
# "sensor") and mangle real names. Users put "{SENSOR}" in a node's
# IoX name to force binary_sensor/sensor classification.
DEFAULT_SENSOR_STRING = "{SENSOR}"
DEFAULT_RESTORE_LIGHT_STATE = False
DEFAULT_PROGRAM_STRING = "HA."
DEFAULT_ENABLE_VARIABLES = True
DEFAULT_ENABLE_PROGRAMS = True
DEFAULT_ENABLE_NETWORKING = False

KEY_ACTIONS = "actions"
KEY_STATUS = "status"

NODE_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.FAN,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
]
NODE_AUX_PROP_PLATFORMS = [
    Platform.BINARY_SENSOR,
    # BUTTON here carries (node, command_id) pairs for plugin-defined
    # zero-arg accept commands (pyisyox classifier's ``result.buttons``),
    # not a property — see helpers._categorize_nodes / button.py.
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
PROGRAM_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.FAN,
    Platform.LOCK,
    Platform.SWITCH,
]
ROOT_NODE_PLATFORMS = [Platform.BUTTON]
VARIABLE_PLATFORMS = [Platform.NUMBER, Platform.SENSOR]

# Platforms used by the rich per-program HA device that surfaces every
# program outside the legacy ``HA.<platform>/<name>/{status,actions}``
# folder convention. One device per program; the program's status,
# running state, schedule timestamps, manual run/stop controls, and
# enable/auto-run toggles each get their own entity.
PROGRAM_DEVICE_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Platforms that classify in parallel with NODE_PLATFORMS — a node placed in
# one of these still falls through to its primary platform classification.
NODE_PARALLEL_PLATFORMS = [Platform.EVENT]

# Set of all platforms used by integration
PLATFORMS = {
    *NODE_PLATFORMS,
    *NODE_AUX_PROP_PLATFORMS,
    *NODE_PARALLEL_PLATFORMS,
    *PROGRAM_PLATFORMS,
    *PROGRAM_DEVICE_PLATFORMS,
    *ROOT_NODE_PLATFORMS,
    *VARIABLE_PLATFORMS,
}

ISY_CONF_UUID = "uuid"

# Special Subnodes for some Insteon Devices
SUBNODE_CLIMATE_COOL = 2
SUBNODE_CLIMATE_HEAT = 3
SUBNODE_DUSK_DAWN = 2
SUBNODE_HEARTBEAT = 4
SUBNODE_LOW_BATTERY = 3
SUBNODE_MOTION_DISABLED = (13, 19)  # Int->13 or Hex->0xD depending on firmware
SUBNODE_NEGATIVE = 2
SUBNODE_TAMPER = (10, 16)  # Int->10 or Hex->0xA depending on firmware

# Generic Insteon Type Categories for Filters
TYPE_CATEGORY_CLIMATE = "5."
TYPE_INSTEON_MOTION = ("16.1.", "16.22.")

# Used for discovery
UDN_UUID_PREFIX = "uuid:"
ISY_URL_POSTFIX = "/desc"

# Special Units of Measure
UOM_ISYV4_DEGREES = "degrees"
UOM_ISYV4_NONE = "n/a"

UOM_ISY_CELSIUS = 1
UOM_ISY_FAHRENHEIT = 2

UOM_8_BIT_RANGE = "100"
UOM_BARRIER = "97"
UOM_DOUBLE_TEMP = "101"
UOM_HVAC_ACTIONS = "66"
UOM_HVAC_MODE_GENERIC = "67"
UOM_HVAC_MODE_INSTEON = "98"
UOM_FAN_MODES = "99"
UOM_INDEX = "25"
UOM_ON_OFF = "2"
UOM_PERCENTAGE = "51"

NODE_AUX_FILTERS: dict[str, Platform] = {
    PROP_ON_LEVEL: Platform.NUMBER,
    PROP_RAMP_RATE: Platform.SELECT,
}

UOM_FRIENDLY_NAME = {
    "1": UnitOfElectricCurrent.AMPERE,
    UOM_ON_OFF: "",  # Binary, no unit
    "3": UnitOfPower.BTU_PER_HOUR,
    "4": UnitOfTemperature.CELSIUS,
    "5": UnitOfLength.CENTIMETERS,
    "6": UnitOfVolume.CUBIC_FEET,
    "7": UnitOfVolumeFlowRate.CUBIC_FEET_PER_MINUTE,
    "8": UnitOfVolume.CUBIC_METERS,
    "9": UnitOfTime.DAYS,
    "10": UnitOfTime.DAYS,
    "12": UnitOfSoundPressure.DECIBEL,
    "13": UnitOfSoundPressure.WEIGHTED_DECIBEL_A,
    "14": DEGREE,
    "16": "macroseismic",
    "17": UnitOfTemperature.FAHRENHEIT,
    "18": UnitOfLength.FEET,
    "19": UnitOfTime.HOURS,
    "20": UnitOfTime.HOURS,
    "21": PERCENTAGE,
    "22": PERCENTAGE,
    "23": UnitOfPressure.INHG,
    "24": UnitOfVolumetricFlux.INCHES_PER_HOUR,
    UOM_INDEX: UOM_INDEX,  # Index type. Use "node.formatted" for value
    "26": UnitOfTemperature.KELVIN,
    "27": "keyword",
    "28": UnitOfMass.KILOGRAMS,
    "29": "kV",
    "30": UnitOfPower.KILO_WATT,
    "31": UnitOfPressure.KPA,
    "32": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "33": UnitOfEnergy.KILO_WATT_HOUR,
    "34": "liedu",
    "35": UnitOfVolume.LITERS,
    "36": LIGHT_LUX,
    "37": "mercalli",
    "38": UnitOfLength.METERS,
    "39": UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
    "40": UnitOfSpeed.METERS_PER_SECOND,
    "41": UnitOfElectricCurrent.MILLIAMPERE,
    "42": UnitOfTime.MILLISECONDS,
    "43": UnitOfElectricPotential.MILLIVOLT,
    "44": UnitOfTime.MINUTES,
    "45": UnitOfTime.MINUTES,
    "46": UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
    "47": UnitOfTime.MONTHS,
    "48": UnitOfSpeed.MILES_PER_HOUR,
    "49": UnitOfSpeed.METERS_PER_SECOND,
    "50": "Ω",
    UOM_PERCENTAGE: PERCENTAGE,
    "52": UnitOfMass.POUNDS,
    "53": "pf",
    "54": CONCENTRATION_PARTS_PER_MILLION,
    "55": "pulse count",
    "57": UnitOfTime.SECONDS,
    "58": UnitOfTime.SECONDS,
    "59": "S/m",
    "60": "m_b",
    "61": "M_L",
    "62": "M_w",
    "63": "M_S",
    "64": "shindo",
    "65": "SML",
    "69": UnitOfVolume.GALLONS,
    "71": UV_INDEX,
    "72": UnitOfElectricPotential.VOLT,
    "73": UnitOfPower.WATT,
    "74": UnitOfIrradiance.WATTS_PER_SQUARE_METER,
    "75": "weekday",
    "76": DEGREE,
    "77": UnitOfTime.YEARS,
    "82": UnitOfLength.MILLIMETERS,
    "83": UnitOfLength.KILOMETERS,
    "85": "Ω",
    "86": "kΩ",
    "87": f"{UnitOfVolume.CUBIC_METERS}/{UnitOfVolume.CUBIC_METERS}",
    "88": "Water activity",
    "89": REVOLUTIONS_PER_MINUTE,
    "90": UnitOfFrequency.HERTZ,
    "91": DEGREE,
    "92": f"{DEGREE} South",
    UOM_8_BIT_RANGE: "",  # Range 0-255, no unit.
    UOM_DOUBLE_TEMP: UOM_DOUBLE_TEMP,
    "102": "kWs",  # Kilowatt Seconds
    "103": CURRENCY_DOLLAR,
    "104": CURRENCY_CENT,
    "105": UnitOfLength.INCHES,
    "106": UnitOfVolumetricFlux.MILLIMETERS_PER_DAY,
    "107": "",  # raw 1-byte unsigned value
    "108": "",  # raw 2-byte unsigned value
    "109": "",  # raw 3-byte unsigned value
    "110": "",  # raw 4-byte unsigned value
    "111": "",  # raw 1-byte signed value
    "112": "",  # raw 2-byte signed value
    "113": "",  # raw 3-byte signed value
    "114": "",  # raw 4-byte signed value
    "116": UnitOfLength.MILES,
    "117": UnitOfPressure.MBAR,
    "118": UnitOfPressure.HPA,
    "119": UnitOfEnergy.WATT_HOUR,
    "120": UnitOfVolumetricFlux.INCHES_PER_DAY,
    "122": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,  # Microgram per cubic meter
    "123": f"bq/{UnitOfVolume.CUBIC_METERS}",  # Becquerel per cubic meter
    "124": f"pCi/{UnitOfVolume.LITERS}",  # Picocuries per liter
    "125": "pH",
    "126": "bpm",  # Beats per Minute
    "127": UnitOfPressure.MMHG,
    "128": "J",
    "129": "BMI",  # Body Mass Index
    "130": "L/h",  # UnitOfVolumeFlowRate.LITERS_PER_HOUR added in HA 2025.7+
    "131": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    "132": "bpm",  # Breaths per minute
    "133": UnitOfFrequency.KILOHERTZ,
    "134": f"{UnitOfLength.METERS}/{UnitOfTime.SECONDS}²",
    "135": UnitOfApparentPower.VOLT_AMPERE,  # Volt-Amp
    "136": UnitOfReactivePower.VOLT_AMPERE_REACTIVE,  # VAR = Volt-Amp Reactive
    "137": "",  # NTP DateTime - Number of seconds since 1900
    "138": UnitOfPressure.PSI,
    "139": DEGREE,  # Degree 0-360
    "140": f"{UnitOfMass.MILLIGRAMS}/{UnitOfVolume.LITERS}",
    "141": "N",  # Netwon
    "142": f"{UnitOfVolume.GALLONS}/{UnitOfTime.SECONDS}",
    "143": UnitOfVolumeFlowRate.GALLONS_PER_MINUTE,
    "144": "gal/h",  # UnitOfVolumeFlowRate.GALLONS_PER_HOUR added in HA 2025.7+
}

UOM_TO_STATES: dict[str, dict[int, str | LockState]] = {
    "11": {  # Deadbolt Status
        0: LockState.UNLOCKED,
        100: LockState.LOCKED,
        101: STATE_UNKNOWN,
        102: STATE_PROBLEM,
    },
    "15": {  # Door Lock Alarm
        1: "master code changed",
        2: "tamper code entry limit",
        3: "escutcheon removed",
        4: "key/manually locked",
        5: "locked by touch",
        6: "key/manually unlocked",
        7: "remote locking jammed bolt",
        8: "remotely locked",
        9: "remotely unlocked",
        10: "deadbolt jammed",
        11: "battery too low to operate",
        12: "critical low battery",
        13: "low battery",
        14: "automatically locked",
        15: "automatic locking jammed bolt",
        16: "remotely power cycled",
        17: "lock handling complete",
        19: "user deleted",
        20: "user added",
        21: "duplicate pin",
        22: "jammed bolt by locking with keypad",
        23: "locked by keypad",
        24: "unlocked by keypad",
        25: "keypad attempt outside schedule",
        26: "hardware failure",
        27: "factory reset",
    },
    UOM_HVAC_ACTIONS: {  # Thermostat Heat/Cool State
        0: HVACAction.IDLE.value,
        1: HVACAction.HEATING.value,
        2: HVACAction.COOLING.value,
        3: HVACAction.FAN.value,
        4: HVACAction.HEATING.value,  # Pending Heat
        5: HVACAction.COOLING.value,  # Pending Cool
        # >6 defined in ISY but not implemented, leaving for future expanision.
        6: HVACAction.IDLE.value,
        7: HVACAction.HEATING.value,
        8: HVACAction.HEATING.value,
        9: HVACAction.COOLING.value,
        10: HVACAction.HEATING.value,
        11: HVACAction.HEATING.value,
    },
    UOM_HVAC_MODE_GENERIC: {  # Thermostat Mode
        0: HVACMode.OFF.value,
        1: HVACMode.HEAT.value,
        2: HVACMode.COOL.value,
        3: HVACMode.AUTO.value,
        4: PRESET_BOOST,
        5: "resume",
        6: HVACMode.FAN_ONLY.value,
        7: "furnace",
        8: HVACMode.DRY.value,
        9: "moist air",
        10: "auto changeover",
        11: "energy save heat",
        12: "energy save cool",
        13: PRESET_AWAY,
        14: HVACMode.AUTO.value,
        15: HVACMode.AUTO.value,
        16: HVACMode.AUTO.value,
    },
    "68": {  # Thermostat Fan Mode
        0: FAN_AUTO,
        1: FAN_ON,
        2: FAN_HIGH,  # Auto High
        3: FAN_HIGH,
        4: FAN_MEDIUM,  # Auto Medium
        5: FAN_MEDIUM,
        6: "circulation",
        7: "humidity circulation",
    },
    "78": {0: STATE_OFF, 100: STATE_ON},  # 0-Off 100-On
    "79": {0: STATE_OPEN, 100: STATE_CLOSED},  # 0-Open 100-Close
    "80": {  # Thermostat Fan Run State
        0: STATE_OFF,
        1: STATE_ON,
        2: "on high",
        3: "on medium",
        4: "circulation",
        5: "humidity circulation",
        6: "right/left circulation",
        7: "up/down circulation",
        8: "quiet circulation",
    },
    "84": {0: SERVICE_LOCK, 1: SERVICE_UNLOCK},  # Secure Mode
    "93": {  # Power Management Alarm
        1: "power applied",
        2: "ac mains disconnected",
        3: "ac mains reconnected",
        4: "surge detection",
        5: "volt drop or drift",
        6: "over current detected",
        7: "over voltage detected",
        8: "over load detected",
        9: "load error",
        10: "replace battery soon",
        11: "replace battery now",
        12: "battery is charging",
        13: "battery is fully charged",
        14: "charge battery soon",
        15: "charge battery now",
    },
    "94": {  # Appliance Alarm
        1: "program started",
        2: "program in progress",
        3: "program completed",
        4: "replace main filter",
        5: "failure to set target temperature",
        6: "supplying water",
        7: "water supply failure",
        8: "boiling",
        9: "boiling failure",
        10: "washing",
        11: "washing failure",
        12: "rinsing",
        13: "rinsing failure",
        14: "draining",
        15: "draining failure",
        16: "spinning",
        17: "spinning failure",
        18: "drying",
        19: "drying failure",
        20: "fan failure",
        21: "compressor failure",
    },
    "95": {  # Home Health Alarm
        1: "leaving bed",
        2: "sitting on bed",
        3: "lying on bed",
        4: "posture changed",
        5: "sitting on edge of bed",
    },
    "96": {  # VOC Level
        1: "clean",
        2: "slightly polluted",
        3: "moderately polluted",
        4: "highly polluted",
    },
    UOM_BARRIER: {  # Barrier Status
        0: STATE_CLOSED,
        100: STATE_OPEN,
        101: STATE_UNKNOWN,
        102: "stopped",
        103: STATE_CLOSING,
        104: STATE_OPENING,
        **{
            b: f"{b} %" for a, b in enumerate(list(range(1, 100)))
        },  # 1-99 are percentage open
    },
    UOM_HVAC_MODE_INSTEON: {  # Insteon Thermostat Mode
        0: HVACMode.OFF.value,
        1: HVACMode.HEAT.value,
        2: HVACMode.COOL.value,
        3: HVACMode.HEAT_COOL.value,
        4: HVACMode.FAN_ONLY.value,
        5: HVACMode.AUTO.value,  # Program Auto
        6: HVACMode.AUTO.value,  # Program Heat-Set @ Local Device Only
        7: HVACMode.AUTO.value,  # Program Cool-Set @ Local Device Only
    },
    UOM_FAN_MODES: {7: FAN_ON, 8: FAN_AUTO},  # Insteon Thermostat Fan Mode
    "115": {  # Most recent On style action taken for lamp control
        0: "on",
        1: "off",
        2: "fade up",
        3: "fade down",
        4: "fade stop",
        5: "fast on",
        6: "fast off",
        7: "triple press on",
        8: "triple press off",
        9: "4x press on",
        10: "4x press off",
        11: "5x press on",
        12: "5x press off",
    },
}

ISY_HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.HEAT,
    HVACMode.COOL,
    HVACMode.HEAT_COOL,
    HVACMode.AUTO,
    HVACMode.FAN_ONLY,
]

HA_HVAC_TO_ISY = {
    HVACMode.OFF: "off",
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.HEAT_COOL: "auto",
    HVACMode.FAN_ONLY: "fan_only",
    HVACMode.AUTO: "program_auto",
}

HA_FAN_TO_ISY = {FAN_ON: "on", FAN_AUTO: "auto"}

TOTAL_INCREASING_DEVICE_CLASSES = {
    SensorDeviceClass.ENERGY,
    SensorDeviceClass.WATER,
    SensorDeviceClass.GAS,
    SensorDeviceClass.PRECIPITATION,
}

BINARY_SENSOR_DEVICE_TYPES_ISY = {
    BinarySensorDeviceClass.MOISTURE: ["16.8.", "16.13.", "16.14."],
    BinarySensorDeviceClass.OPENING: [
        "16.9.",
        "16.6.",
        "16.7.",
        "16.2.",
        "16.17.",
        "16.20.",
        "16.21.",
    ],
    BinarySensorDeviceClass.MOTION: ["16.1.", "16.4.", "16.5.", "16.3.", "16.22."],
}

BINARY_SENSOR_DEVICE_TYPES_ZWAVE = {
    BinarySensorDeviceClass.SAFETY: ["137", "172", "176", "177", "178"],
    BinarySensorDeviceClass.SMOKE: ["138", "156"],
    BinarySensorDeviceClass.PROBLEM: ["148", "149", "157", "158", "164", "174", "175"],
    BinarySensorDeviceClass.GAS: ["150", "151"],
    BinarySensorDeviceClass.SOUND: ["153"],
    BinarySensorDeviceClass.COLD: ["152", "168"],
    BinarySensorDeviceClass.HEAT: ["154", "166", "167"],
    BinarySensorDeviceClass.MOISTURE: ["159", "169"],
    BinarySensorDeviceClass.DOOR: ["160"],
    BinarySensorDeviceClass.BATTERY: ["162"],
    BinarySensorDeviceClass.MOTION: ["155"],
    BinarySensorDeviceClass.VIBRATION: ["173"],
}


SCHEME_HTTP = "http"
HTTP_PORT = 80
SCHEME_HTTPS = "https"
HTTPS_PORT = 443

BACKLIGHT_MEMORY_FILTER = {"memory": DEV_BL_ADDR, "cmd1": DEV_CMD_MEMORY_WRITE}
