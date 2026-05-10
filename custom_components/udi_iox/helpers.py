"""Node-to-platform classification for the IoX integration.

Two-tier strategy that mirrors pyisyox 6's design:

1. **Native nodes** (Insteon, Z-Wave, Zigbee, X10) are classified by
   the type-based introspection that pyisyox exposes on ``Node``:
   ``is_thermostat`` / ``is_lock`` / ``is_fan`` / ``is_dimmable`` /
   ``is_battery_node``. No hardcoded type-prefix tables here — pyisyox
   owns that knowledge.
2. **Plugin nodes** (PG3 node-server, ``protocol == "node_server"``)
   fall back to :func:`pyisyox.classify` against the plugin's nodedef.
   That returns a :class:`ClassificationResult` with a controllable
   platform plus per-property reading platforms.

Properties that aren't covered by the primary platform fan out into
``aux_properties`` (number / select / sensor / binary_sensor) using
the same loop the legacy XML-filter code used. The loop uses
``Node.properties`` (the v6 rename of ``aux_properties``) and
:class:`NodePropertyValue`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, cast

from homeassistant.const import ATTR_MANUFACTURER, ATTR_MODEL, Platform
from homeassistant.helpers.entity import DeviceInfo
from pyisyox import (
    ClassificationResult,
    ControllablePlatform,
    Controller,
    Group,
    Node,
    Reading,
    ReadingPlatform,
    classify,
)
from pyisyox.constants import (
    CMD_BACKLIGHT,
    PROP_BUSY,
    PROP_COMMS_ERROR,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
    PROP_STATUS,
    Protocol,
)

from .const import (
    _LOGGER,
    CONF_IGNORE_STRING,
    CONF_SENSOR_STRING,
    DEFAULT_IGNORE_STRING,
    DEFAULT_PROGRAM_STRING,
    DEFAULT_SENSOR_STRING,
    DOMAIN,
    KEY_ACTIONS,
    KEY_STATUS,
    NODE_AUX_FILTERS,
    NODE_PARALLEL_PLATFORMS,
    PROGRAM_PLATFORMS,
    UOM_DOUBLE_TEMP,
    UOM_ISYV4_DEGREES,
)
from .models import IsyData

ROOT_AUX_CONTROLS = {PROP_ON_LEVEL, PROP_RAMP_RATE}
SKIP_AUX_PROPS = {PROP_BUSY, PROP_COMMS_ERROR, PROP_STATUS, *ROOT_AUX_CONTROLS}

# UOMs that mark a property as binary (matches pyisyox classifier's
# _BINARY_UOMS but kept here so the aux-property fan-out stays
# self-contained — pyisyox doesn't export it publicly).
BINARY_UOMS = frozenset({"2", "78", "79"})

#: Map a controllable platform from the classifier to the HA platform
#: enum we register entities under.
_CONTROLLABLE_TO_HA_PLATFORM: dict[ControllablePlatform, Platform] = {
    ControllablePlatform.LIGHT: Platform.LIGHT,
    ControllablePlatform.SWITCH: Platform.SWITCH,
    ControllablePlatform.CLIMATE: Platform.CLIMATE,
    ControllablePlatform.LOCK: Platform.LOCK,
    ControllablePlatform.COVER: Platform.COVER,
    # alarm_control_panel is in pyisyox but the integration doesn't
    # surface it yet — fall through to sensor for now.
}

_READING_TO_HA_PLATFORM: dict[ReadingPlatform, Platform] = {
    ReadingPlatform.SENSOR: Platform.SENSOR,
    ReadingPlatform.BINARY_SENSOR: Platform.BINARY_SENSOR,
}


def _is_device_root(node: Node) -> bool:
    """A node without a parent_address is a physical device root.

    pyisyox 6 dropped the explicit ``is_device_root`` flag; subnodes
    expose ``parent_address`` pointing at the root.
    """
    return node.parent_address is None


def _primary_platform_for_native(node: Node) -> Platform:
    """Pick the HA platform for a native (Insteon/Z-Wave/Zigbee) node."""
    if node.is_thermostat:
        return Platform.CLIMATE
    if node.is_lock:
        return Platform.LOCK
    if node.is_fan:
        return Platform.FAN
    if node.is_dimmable:
        return Platform.LIGHT
    return Platform.SWITCH


def _classify_plugin_node(
    controller: Controller, node: Node
) -> ClassificationResult | None:
    """Run the pyisyox classifier against a plugin node's nodedef."""
    nodedef = node.nodedef
    if nodedef is None:
        # Profile not loaded yet, or nodedef id unknown — skip until reload.
        _LOGGER.debug(
            "Skipping plugin node %s: nodedef %r not present in profile",
            node.address,
            node.nodedef_id,
        )
        return None
    return classify(
        nodedef,
        find_editor=lambda editor_id: controller.profile.find_editor(
            editor_id, node.family_id, node.instance_id
        ),
    )


def _fan_out_readings(
    isy_data: IsyData, node: Node, readings: list[Reading]
) -> None:
    """Append each plugin-classified reading as an aux-property entity."""
    for reading in readings:
        ha_platform = _READING_TO_HA_PLATFORM[reading.platform]
        isy_data.aux_properties[ha_platform].append((node, reading.property.id))


def _fan_out_native_aux(isy_data: IsyData, node: Node) -> None:
    """Surface native-node aux properties as sensor / binary_sensor entities."""
    for control, prop in node.properties.items():
        if control in SKIP_AUX_PROPS:
            continue
        platform = (
            Platform.BINARY_SENSOR if prop.uom in BINARY_UOMS else Platform.SENSOR
        )
        isy_data.aux_properties[platform].append((node, control))


def _generate_device_info(controller: Controller, node: Node, host: str) -> DeviceInfo:
    """Generate the device info for a root node device."""
    uuid = controller.config.uuid

    # node.protocol is a plain str ("insteon", "zwave", ...); title-case
    # for display.
    manufacturer = (
        node.protocol.replace("_", " ").title() if node.protocol else "Unknown"
    )
    device_info = DeviceInfo(
        identifiers={(DOMAIN, f"{uuid}_{node.address}")},
        manufacturer=manufacturer,
        name=node.name,
        via_device=(DOMAIN, uuid),
        configuration_url=host,
    )

    model: str = str(node.address).rpartition(" ")[0] or node.address
    if node.nodedef_id:
        model += f": {node.nodedef_id}"
    if node.type:
        model += f" ({node.type})"
    device_info[ATTR_MODEL] = model
    device_info[ATTR_MANUFACTURER] = manufacturer

    return device_info


def _categorize_nodes(
    isy_data: IsyData,
    nodes: dict[str, Node],
    isy_options: MappingProxyType[str, Any],
    *,
    controller: Controller | None = None,
    host: str = "",
) -> None:
    """Sort nodes onto HA platforms.

    ``controller`` and ``host`` are required to resolve nodedefs from
    the live profile and stamp DeviceInfo; the defaults exist only so
    older test fixtures that don't pass them no-op cleanly.
    """
    if controller is None:
        return

    ignore_identifier = isy_options.get(CONF_IGNORE_STRING, DEFAULT_IGNORE_STRING)
    sensor_identifier = isy_options.get(CONF_SENSOR_STRING, DEFAULT_SENSOR_STRING)

    # Groups (scenes) come from controller.groups, not the node dict.
    for group in controller.groups.values():
        if ignore_identifier in group.name:
            continue
        isy_data.groups.append(group)

    for node in nodes.values():
        if ignore_identifier in node.name:
            continue

        if _is_device_root(node):
            isy_data.devices[node.address] = _generate_device_info(
                controller, node, host
            )
            isy_data.root_nodes[Platform.BUTTON].append(node)
            isy_data.aux_properties[Platform.SENSOR].append((node, PROP_COMMS_ERROR))

            if node.is_dimmable:
                for control in ROOT_AUX_CONTROLS.intersection(node.properties):
                    platform = NODE_AUX_FILTERS[control]
                    isy_data.aux_properties[platform].append((node, control))

        # User-forced sensor classification short-circuits everything
        # else — keep the v3 ergonomics.
        if sensor_identifier in node.name:
            platform = (
                Platform.BINARY_SENSOR
                if any(
                    prop.uom in BINARY_UOMS for prop in node.properties.values()
                )
                else Platform.SENSOR
            )
            isy_data.nodes[platform].append(node)
            continue

        # Plugin nodes: defer to the pyisyox classifier.
        if node.protocol == Protocol.NODE_SERVER:
            result = _classify_plugin_node(controller, node)
            if result is None:
                continue
            if result.controllable is not None:
                ha_platform = _CONTROLLABLE_TO_HA_PLATFORM.get(
                    result.controllable, Platform.SENSOR
                )
                isy_data.nodes[ha_platform].append(node)
            _fan_out_readings(isy_data, node, result.readings)
            # NODE_PARALLEL_PLATFORMS (e.g. EVENT) — pyisyox classifier
            # surfaces emitted commands as triggers; the consumer wires
            # those into Platform.EVENT.
            if result.triggers and Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                isy_data.nodes[Platform.EVENT].append(node)
            continue

        # Native nodes: type-based introspection.
        primary = _primary_platform_for_native(node)

        # KeypadLinc-style sub-buttons (LED-only sub-nodes that fall
        # through ``_primary_platform_for_native`` to SWITCH) don't
        # control a load — only their own LED. Surface them as EVENT
        # only, drop the would-be switch entity entirely.
        is_subnode_button = (
            node.parent_address is not None
            and primary == Platform.SWITCH
            and node.protocol == Protocol.INSTEON
        )
        if is_subnode_button:
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                isy_data.nodes[Platform.EVENT].append(node)
            continue

        isy_data.nodes[primary].append(node)
        _fan_out_native_aux(isy_data, node)

        # Parallel: native Insteon LIGHT/SWITCH nodes also emit
        # press/fast/fade events that the EVENT platform surfaces.
        if (
            Platform.EVENT in NODE_PARALLEL_PLATFORMS
            and primary in (Platform.LIGHT, Platform.SWITCH)
            and node.protocol == Protocol.INSTEON
        ):
            isy_data.nodes[Platform.EVENT].append(node)


def _categorize_programs(isy_data: IsyData, programs: list[dict]) -> None:
    """Categorize the controller's programs onto HA platforms.

    Programs are exposed as raw dicts (whatever ``/api/programs``
    returns — at minimum ``id``, ``name``, ``enabled``, ``path``).
    Classification walks the ``HA.<platform>/`` folder convention the
    legacy integration established.
    """
    by_path: dict[str, dict] = {p.get("path", ""): p for p in programs if "path" in p}

    for platform in PROGRAM_PLATFORMS:
        folder_prefix = f"{DEFAULT_PROGRAM_STRING}{platform}/"
        entities: dict[str, dict] = {
            path.partition(folder_prefix)[2]: program
            for path, program in by_path.items()
            if folder_prefix in path
        }

        if not entities:
            continue

        status_programs = {
            path.rstrip(f"/{KEY_STATUS}"): status
            for path, status in entities.items()
            if path.endswith(KEY_STATUS)
        }
        action_programs = {
            path.rstrip(f"/{KEY_ACTIONS}"): action
            for path, action in entities.items()
            if path.endswith(KEY_ACTIONS)
        }

        for name, program in status_programs.items():
            if platform != Platform.BINARY_SENSOR and name not in action_programs:
                _LOGGER.warning(
                    "Program %s entity '%s' not loaded, missing actions program",
                    platform,
                    name,
                )
            isy_data.programs[platform].append(
                (name, program, action_programs.get(name))
            )


def _categorize_variables(
    isy_data: IsyData, variables: dict[str, list[dict]]
) -> None:
    """Add controller variables as Number platform entities."""
    numbers = isy_data.variables[Platform.NUMBER]
    for type_id, entries in variables.items():
        for variable in entries:
            # Stamp the type id onto the dict so unique-ids can use it
            # (variables come keyed by type id, but the dict itself
            # doesn't carry that.)
            variable.setdefault("type", type_id)
            numbers.append(variable)


def convert_isy_value_to_hass(
    value: float | None,
    uom: str | list | None,
    precision: int | str,
    fallback_precision: int | None = None,
) -> float | int | None:
    """Fix IoX-reported values.

    IoX provides float values as an integer + precision component;
    shift the decimal place left by precision. Insteon thermostats
    report temperature in 0.5°-precision as 2× the temp.
    """
    if value is None:
        return None
    if uom in (UOM_DOUBLE_TEMP, UOM_ISYV4_DEGREES):
        return round(float(value) / 2.0, 1)
    if precision not in ("0", 0):
        return cast(float, round(float(value) / 10 ** int(precision), int(precision)))
    if fallback_precision:
        return round(float(value), fallback_precision)
    return value
