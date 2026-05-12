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
    CMD_BEEP,
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
    UOM_ISYV4_DEGREES,
)
from .editor_classification import BINARY_UOMS, platform_for_control, resolve_editor
from .models import IsyData

#: ST / OL / RR — the dimmer's own controls. ST is the primary entity;
#: OL / RR surface as aux NUMBER / SELECT entities via the nodedef's
#: ``OL`` / ``RR`` accept commands (see ``_fan_out_commands``), so they
#: must be excluded from the read-only sensor fan-out.
ROOT_AUX_CONTROLS = {PROP_ON_LEVEL, PROP_RAMP_RATE}
SKIP_AUX_PROPS = {PROP_BUSY, PROP_COMMS_ERROR, PROP_STATUS, *ROOT_AUX_CONTROLS}


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


def _classify_node(controller: Controller, node: Node) -> ClassificationResult | None:
    """Run the pyisyox classifier against a node's nodedef.

    Same shape for native (Insteon / Z-Wave / Zigbee) and PG3 plugin
    nodedefs — the classifier reads links / accepts / properties, not a
    protocol flag. ``None`` when the nodedef isn't in the loaded profile
    (a node that joined after load, or an unknown nodedef id — wait for
    a reload).
    """
    nodedef = node.nodedef
    if nodedef is None:
        _LOGGER.debug(
            "No nodedef for %s (%r) in the loaded profile; skipping classify",
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


#: Accept commands the integration surfaces through a *dedicated* entity
#: rather than the generic command fan-out, keyed by the protocol whose
#: device-root scaffold provides that entity — skip them here so they
#: aren't double-created. (``QUERY`` is already dropped upstream by
#: ``pyisyox.classify``; Insteon's ``BEEP`` gets the bespoke
#: ``ISYNodeBeepButtonEntity``. A plugin nodedef that declares ``BEEP``
#: still gets the generic button — there's no scaffolded one for it.)
_DEDICATED_COMMANDS_BY_PROTOCOL: dict[str, frozenset[str]] = {
    Protocol.INSTEON: frozenset({CMD_BEEP}),
}


def _fan_out_commands(
    isy_data: IsyData,
    node: Node,
    controller: Controller,
    result: ClassificationResult,
) -> None:
    """Surface a node's accept commands as input / button aux entities.

    Driven by :func:`pyisyox.classify` (it already drops ``QUERY`` and
    the verbs the controllable platform claims, e.g. ``DON`` / ``DOF`` /
    ``BRT`` on a light):

    * ``parameterized_commands`` (carry a required parameter — ``OL``,
      ``RR``, ``BL`` backlight, plugin setters) → a NUMBER / SELECT
      entity, the platform chosen from the parameter's editor via
      :func:`platform_for_control`. (Bool editors resolve to SWITCH but
      aren't surfaced yet — no aux-command switch entity exists.) A
      parameter's ``init`` names the property the value lives on:
      ``init == "ST"`` means the controllable platform owns it (skip);
      otherwise the entity is created regardless of whether the device
      has *reported* that property yet — a nodedef property is declared
      to exist, so the entity simply reads ``unknown`` until the first
      value frame (it's subscribed). No ``init`` means there's no
      backing property at all → the entity is assumed-state (``BL``
      backlight, plugin write-only setters). A command whose editor
      can't be resolved falls back to :data:`NODE_AUX_FILTERS` if it's
      listed there, else is skipped — the ``send_node_command`` service
      still reaches it.
    * ``buttons`` (no required parameter) → one BUTTON entity each
      (``WDU`` "Write Changes", plugin ``DISCOVER`` …), minus the ones
      the integration ships a dedicated entity for.
    """
    dedicated = _DEDICATED_COMMANDS_BY_PROTOCOL.get(node.protocol or "", frozenset())
    for cmd in result.parameterized_commands:
        if cmd.id in dedicated:
            continue
        if any(p.init == PROP_STATUS for p in cmd.parameters):
            continue
        editor = resolve_editor(controller, node, cmd.id)
        prop = node.properties.get(cmd.id)
        platform = platform_for_control(
            editor, prop.uom if prop is not None else None, writable=True
        )
        if platform is Platform.SWITCH:
            # The SWITCH platform's aux path is the device-enable switch,
            # not a command sender — a bool *command* needs its own
            # entity class. Until that exists, leave it to the service.
            _LOGGER.debug(
                "Bool aux command %s/%s not surfaced as a switch yet; use the service",
                node.address,
                cmd.id,
            )
            continue
        if platform is None:
            platform = NODE_AUX_FILTERS.get(cmd.id)
        if platform is None:
            _LOGGER.debug(
                "No editor-resolved platform for %s/%s; leaving it to the service",
                node.address,
                cmd.id,
            )
            continue
        isy_data.aux_properties[platform].append((node, cmd.id))
    for cmd in result.buttons:
        if cmd.id in dedicated:
            continue
        isy_data.aux_properties[Platform.BUTTON].append((node, cmd.id))


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

        # Classifier output drives the controllable platform (plugins),
        # the readings (plugins), and the accept-command aux entities
        # (every node). ``None`` when the nodedef isn't loaded — native
        # nodes still get their introspection-based primary platform;
        # plugin nodes are skipped.
        result = _classify_node(controller, node)

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

        # Plugin nodes: the classifier owns the controllable platform +
        # readings; native nodes use type-based introspection for the
        # primary. Either way the accept-command fan-out is the same.
        if node.protocol == Protocol.NODE_SERVER:
            if result is None:
                continue
            if result.controllable is not None:
                ha_platform = _CONTROLLABLE_TO_HA_PLATFORM.get(
                    result.controllable, Platform.SENSOR
                )
                isy_data.nodes[ha_platform].append(node)
            _fan_out_readings(isy_data, node, result.readings)
            _fan_out_commands(isy_data, node, controller, result)
            # NODE_PARALLEL_PLATFORMS (e.g. EVENT) — the classifier's
            # ``triggers`` list IS the nodedef's ``cmds.sends``; wire it
            # onto the EVENT platform so plugin verbs (DOORBELL_PRESS,
            # MOTION_ON, …) surface, not just the Insteon set.
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                _register_event_node(isy_data, node, list(result.triggers))
            continue

        # Native nodes: type-based introspection for the primary.
        primary = _primary_platform_for_native(node)

        # KeypadLinc-style sub-buttons (LED-only sub-nodes that fall
        # through ``_primary_platform_for_native`` to SWITCH) don't
        # control a load — only their own LED. Surface them as EVENT
        # only, drop the would-be switch entity (and its aux commands).
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
        # Accept-command aux entities attach to the device, so only the
        # device root carries them (a native sub-node — FanLinc fan side,
        # KeypadLinc sub-button — has no HA device of its own; its
        # commands belong to the primary). Plugin children each have
        # their own device, so the plugin branch above isn't gated.
        if result is not None and _is_device_root(node):
            _fan_out_commands(isy_data, node, controller, result)

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
