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
    Node,
    Program,
    Reading,
    ReadingPlatform,
    Variable,
    classify,
)
from pyisyox.constants import (
    BACKLIGHT_SUPPORT,
    CMD_BACKLIGHT,
    PROP_BUSY,
    PROP_COMMS_ERROR,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
    PROP_STATUS,
    TAG_ENABLED,
    Protocol,
)
from pyisyox.schema.nodedef import Command

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
    UOM_INDEX,
    UOM_ISYV4_DEGREES,
)
from .editor_classification import BINARY_UOMS, platform_for_control, resolve_editor
from .models import IsyData

ROOT_AUX_CONTROLS = {PROP_ON_LEVEL, PROP_RAMP_RATE}
SKIP_AUX_PROPS = {PROP_BUSY, PROP_COMMS_ERROR, PROP_STATUS, *ROOT_AUX_CONTROLS}


def _aux_platform_for(
    controller: Controller,
    node: Node,
    control: str,
    *,
    writable: bool,
    fallback: Platform,
) -> Platform:
    """HA platform for an aux ``control``, decided from its editor shape.

    Resolves the editor governing ``control`` (the nodedef property's
    for settable status props, the accept-command parameter's for
    command-only controls like backlight), disambiguates a multi-range
    editor by the control's live UOM, and runs
    :func:`platform_for_control`. Falls back to ``fallback`` (the legacy
    static map / UOM heuristic) when the editor can't be resolved or
    doesn't pin a platform down.
    """
    editor = resolve_editor(controller, node, control)
    prop = node.properties.get(control)
    prop_uom = prop.uom if prop is not None else None
    return platform_for_control(editor, prop_uom, writable=writable) or fallback


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


def _add_backlight_if_supported(
    isy_data: IsyData, node: Node, controller: Controller
) -> None:
    """Append a backlight aux entity for nodedefs that support it.

    pyisyox's :data:`BACKLIGHT_SUPPORT` still gates *which* nodedefs get
    a backlight entity (it's an accept-only command — no backing
    property — so there's no live value to key on). The HA platform,
    though, comes from the ``BL`` command's editor: ``I_BL`` (UOM 51,
    0-100) → NUMBER, ``I_BL_KP`` (UOM 25, indexed on/off pairs) →
    SELECT. The legacy ``BACKLIGHT_SUPPORT`` UOM is the fallback when
    the editor can't be resolved.
    """
    legacy_uom = BACKLIGHT_SUPPORT.get(node.nodedef_id)
    if legacy_uom is None:
        return
    platform = _aux_platform_for(
        controller,
        node,
        CMD_BACKLIGHT,
        writable=True,
        fallback=Platform.SELECT if legacy_uom == UOM_INDEX else Platform.NUMBER,
    )
    isy_data.aux_properties[platform].append((node, CMD_BACKLIGHT))


def _is_device_root(node: Node) -> bool:
    """A node without a primary_address is a physical device root.

    Sub-nodes of multi-button devices (KeypadLinc, RemoteLinc, FanLinc
    sides) expose ``primary_address`` pointing at the device primary;
    primaries themselves return ``None``.

    Used to gate the root-only scaffold (BUTTON entity, comms_error
    sensor, enable switch) — those only make sense on a physical
    device's primary node, not on plugin-side logical children.
    """
    return node.primary_address is None


def _has_own_device(node: Node) -> bool:
    """Whether this node should have its own HA :class:`DeviceInfo`.

    True for top-level roots AND for node-server plugin children: each
    plugin node is a distinct logical device on the upstream service
    (e.g. each Flume sensor / hub under a Flume controller node), so
    we mirror the eisy UI by giving each its own HA device card rather
    than folding every child's aux properties under the controller —
    which produces duplicate "Current" / "Leak Detected" entities the
    user can't tell apart.

    False for Insteon / Z-Wave physical sub-nodes (KeypadLinc buttons,
    FanLinc fan-vs-light sides): those are sub-parts of one physical
    device and stay folded under the primary.
    """
    return node.primary_address is None or node.protocol == Protocol.NODE_SERVER


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


def _node_trigger_commands(node: Node) -> list[Command]:
    """The control verbs a node emits, from its nodedef's ``cmds.sends``.

    Empty when the nodedef isn't resolved or declares no sent commands —
    such nodes get no EVENT entity (they have nothing to fire).
    """
    nodedef = node.nodedef
    return list(nodedef.cmds.sends) if nodedef is not None else []


def _register_event_node(
    isy_data: IsyData, node: Node, trigger_cmds: list[Command]
) -> None:
    """Route ``node`` onto the EVENT platform with its trigger vocabulary.

    No-ops on an empty command list. Caller must have confirmed
    ``Platform.EVENT`` is an enabled parallel platform.
    """
    if not trigger_cmds:
        return
    isy_data.nodes[Platform.EVENT].append(node)
    isy_data.node_triggers[node.address] = trigger_cmds


def _fan_out_readings(isy_data: IsyData, node: Node, readings: list[Reading]) -> None:
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
    """Generate the device info for a node that gets its own HA device.

    For node-server plugin children (``primary_address`` set + protocol
    is ``NODE_SERVER``), anchor ``via_device`` on the controller node
    instead of the eisy root so HA renders the hub→sensor hierarchy
    (eisy → FlumeWater controller → Flume Sensor 7061…).
    """
    uuid = controller.config.uuid

    # node.protocol is a plain str ("insteon", "zwave", ...); title-case
    # for display.
    manufacturer = (
        node.protocol.replace("_", " ").title() if node.protocol else "Unknown"
    )
    if node.protocol == Protocol.NODE_SERVER and node.primary_address is not None:
        via_device = (DOMAIN, f"{uuid}_{node.primary_address}")
    else:
        via_device = (DOMAIN, uuid)
    device_info = DeviceInfo(
        identifiers={(DOMAIN, f"{uuid}_{node.address}")},
        manufacturer=manufacturer,
        name=node.name,
        via_device=via_device,
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

        # DeviceInfo population is wider than the root scaffold: plugin
        # children also get their own device so the eisy-side hierarchy
        # is preserved in HA.
        if _has_own_device(node):
            isy_data.devices[node.address] = _generate_device_info(
                controller, node, host
            )

        if _is_device_root(node):
            isy_data.root_nodes[Platform.BUTTON].append(node)
            # comms_error (ERR) is an Insteon PLM-side counter; native
            # Insteon nodes carry it on ``properties``, plugin/Z-Wave/
            # Zigbee roots don't. Gate on actual presence rather than
            # protocol so any node that exposes ERR gets the sensor and
            # node-server controllers don't sprout a perpetual
            # "Unavailable".
            if PROP_COMMS_ERROR in node.properties:
                isy_data.aux_properties[Platform.SENSOR].append(
                    (node, PROP_COMMS_ERROR)
                )
            # Per-device enable/disable switch — mirrors hacs-isy994's
            # exposure of the controller-side enabled flag, useful for
            # automations that need to mute a flaky node without removing
            # it from the controller.
            if hasattr(node, TAG_ENABLED):
                isy_data.aux_properties[Platform.SWITCH].append((node, TAG_ENABLED))

            if node.is_dimmable:
                for control in ROOT_AUX_CONTROLS.intersection(node.properties):
                    platform = _aux_platform_for(
                        controller,
                        node,
                        control,
                        writable=True,
                        fallback=NODE_AUX_FILTERS[control],
                    )
                    isy_data.aux_properties[platform].append((node, control))

            _add_backlight_if_supported(isy_data, node, controller)

        # User-forced sensor classification short-circuits everything
        # else — keep the v3 ergonomics.
        if sensor_identifier in node.name:
            platform = (
                Platform.BINARY_SENSOR
                if any(prop.uom in BINARY_UOMS for prop in node.properties.values())
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
            # Zero-arg plugin accept commands (parameterless or all-params
            # optional, e.g. DISCOVER / BEEP) → one button entity each.
            # Commands with a required parameter (result.parameterized_commands)
            # need editor-driven input entities — out of scope here; the
            # send_node_command service covers them in the meantime.
            for cmd in result.buttons:
                isy_data.aux_properties[Platform.BUTTON].append((node, cmd.id))
            # NODE_PARALLEL_PLATFORMS (e.g. EVENT) — the classifier's
            # ``triggers`` list IS the nodedef's ``cmds.sends``; wire it
            # onto the EVENT platform so plugin verbs (DOORBELL_PRESS,
            # MOTION_ON, …) surface, not just the Insteon set.
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                _register_event_node(isy_data, node, list(result.triggers))
            continue

        # Native nodes: type-based introspection.
        primary = _primary_platform_for_native(node)

        # KeypadLinc-style sub-buttons (LED-only sub-nodes that fall
        # through ``_primary_platform_for_native`` to SWITCH) don't
        # control a load — only their own LED. Surface them as EVENT
        # only, drop the would-be switch entity entirely.
        is_subnode_button = (
            node.primary_address is not None
            and primary == Platform.SWITCH
            and node.protocol == Protocol.INSTEON
        )
        if is_subnode_button:
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                _register_event_node(isy_data, node, _node_trigger_commands(node))
            continue

        isy_data.nodes[primary].append(node)
        _fan_out_native_aux(isy_data, node)

        # Parallel: native Insteon LIGHT/SWITCH nodes whose nodedef
        # declares sent verbs also feed the EVENT platform.
        if (
            Platform.EVENT in NODE_PARALLEL_PLATFORMS
            and primary in (Platform.LIGHT, Platform.SWITCH)
            and node.protocol == Protocol.INSTEON
        ):
            _register_event_node(isy_data, node, _node_trigger_commands(node))


def _categorize_programs(isy_data: IsyData, programs: dict[str, Program]) -> None:
    """Categorize the controller's programs onto HA platforms.

    Walks the legacy ``HA.<platform>/<name>/<status|actions>`` folder
    convention pyisy 3.x consumers established. ``Program.path`` is
    reconstructed from the ``parentId`` chain by pyisyox at parse
    time, so this side just splits on the platform prefix and pairs
    ``status`` / ``actions`` programs by their inner name.
    """
    by_path: dict[str, Program] = {p.path: p for p in programs.values() if p.path}

    for platform in PROGRAM_PLATFORMS:
        folder_prefix = f"{DEFAULT_PROGRAM_STRING}{platform}/"
        entities: dict[str, Program] = {
            path.partition(folder_prefix)[2]: program
            for path, program in by_path.items()
            if folder_prefix in path
        }

        if not entities:
            continue

        status_programs = {
            path.removesuffix(f"/{KEY_STATUS}"): status
            for path, status in entities.items()
            if path.endswith(f"/{KEY_STATUS}")
        }
        action_programs = {
            path.removesuffix(f"/{KEY_ACTIONS}"): action
            for path, action in entities.items()
            if path.endswith(f"/{KEY_ACTIONS}")
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
    isy_data: IsyData, variables: dict[str, dict[str, Variable]]
) -> None:
    """Add controller variables as Number platform entities."""
    numbers = isy_data.variables[Platform.NUMBER]
    for entries in variables.values():
        numbers.extend(entries.values())


def convert_isy_value_to_hass(
    value: float | str | None,
    uom: str | list | None,
    precision: int | str,
    fallback_precision: int | None = None,
) -> float | int | None:
    """Fix IoX-reported values.

    IoX provides float values as an integer + precision component;
    shift the decimal place left by precision. Insteon thermostats
    report temperature in 0.5°-precision as 2x the temp.

    ``NodePropertyValue.value`` arrives as a string from both wire shapes
    (``/api/nodes`` JSON and ``/rest/status`` XML), so coerce to ``float``
    once at entry. Returns ``None`` when the string isn't numeric (which
    plugin nodes can legitimately do for non-numeric readings — those
    should be surfaced via ``target.formatted`` upstream).
    """
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if uom in (UOM_DOUBLE_TEMP, UOM_ISYV4_DEGREES):
        return round(numeric / 2.0, 1)
    if precision not in ("0", 0):
        return cast(float, round(numeric / 10 ** int(precision), int(precision)))
    if fallback_precision:
        return round(numeric, fallback_precision)
    return numeric
