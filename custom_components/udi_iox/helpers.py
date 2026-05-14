"""Sort runtime nodes onto HA platforms.

Native nodes use type-based introspection on ``pyisyox.Node``; plugin
nodes go through ``pyisyox.classify``. Properties not covered by the
primary platform fan out into ``aux_properties``.
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

    Gates root-only scaffold (BUTTON, comms_error, enable switch).
    """
    return node.primary_address is None


def _has_own_device(node: Node) -> bool:
    """Whether this node should have its own HA :class:`DeviceInfo`.

    Node-server plugin children get their own card (each is a distinct
    logical device upstream — folding would duplicate aux entities
    indistinguishably). Insteon/Z-Wave physical sub-nodes stay folded
    under the primary.
    """
    return node.primary_address is None or node.protocol == Protocol.NODE_SERVER


def _primary_platform_for_native(
    node: Node, result: ClassificationResult | None
) -> Platform | None:
    """Pick the HA platform for a native (Insteon/Z-Wave/Zigbee) node.

    The type-introspection helpers (``is_thermostat`` / ``is_lock`` /
    ``is_fan``) win first — they read the Insteon type triple, which
    the nodedef-only classifier can't see (a DoorLock nodedef looks
    like SWITCH-shaped at the cmds level; only the type ``111.5.0.0``
    proves it's a lock). Everything else routes through the
    classifier's ``controllable`` decision so nodes with no
    controllable surface (RemoteLinc2_ADV scene buttons, IMETER_SOLO
    energy meters, …) correctly resolve to **no primary entity**
    rather than a broken switch / light entity. ``None`` means
    "register only as EVENT (if it sends verbs) and on aux platforms";
    callers handle that fallthrough.

    The defensive ``result is None → SWITCH`` branch preserves the
    historical behavior on a nodedef-not-yet-loaded race so the
    integration doesn't drop a node it can't classify.
    """
    if node.is_thermostat:
        return Platform.CLIMATE
    if node.is_lock:
        return Platform.LOCK
    if node.is_fan:
        return Platform.FAN
    if result is None:
        # Nodedef not loaded yet — keep the historical SWITCH default
        # rather than dropping the node entirely.
        return Platform.SWITCH
    if result.controllable is None:
        return None
    return _CONTROLLABLE_TO_HA_PLATFORM.get(result.controllable, Platform.SWITCH)


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

#: Accept commands surfaced **only** as service calls, never as aux
#: entities. Z-Wave ``CONFIG``'s ``(NUM, VAL)`` pair editor can't
#: express the parameter's third *byte size* arg, so the multi-byte
#: write path needs ``/rest/zwave/.../parameters/set/{n}/{v}/{sz}``
#: via the ``udi_iox.set_zwave_parameter`` service. Insteon
#: ``CONFIG`` (rare; single byte) stays as a slider.
_SERVICE_ONLY_COMMANDS_BY_PROTOCOL: dict[str, frozenset[str]] = {
    Protocol.ZWAVE: frozenset({"CONFIG"}),
}


def _fan_out_commands(
    isy_data: IsyData,
    node: Node,
    controller: Controller,
    result: ClassificationResult,
) -> None:
    """Surface a node's accept commands as input / button aux entities.

    * ``parameterized_commands`` → NUMBER / SELECT via editor →
      ``platform_for_control``. ``init == "ST"`` means controllable
      owns it (skip); no ``init`` → assumed-state (backlight, plugin
      write-only setters); editor unresolved → ``NODE_AUX_FILTERS``
      fallback or service-only.
    * ``buttons`` → one BUTTON each (``WDU``, plugin ``DISCOVER``…),
      minus the ones with a dedicated entity class.
    """
    protocol_key = node.protocol or ""
    dedicated = _DEDICATED_COMMANDS_BY_PROTOCOL.get(protocol_key, frozenset())
    service_only = _SERVICE_ONLY_COMMANDS_BY_PROTOCOL.get(protocol_key, frozenset())
    skipped = dedicated | service_only
    for cmd in result.parameterized_commands:
        if cmd.id in skipped:
            continue
        if any(p.init == PROP_STATUS for p in cmd.parameters):
            continue
        editor = resolve_editor(controller, node, cmd.id)
        prop = node.properties.get(cmd.id)
        platform = platform_for_control(
            editor, prop.uom if prop is not None else None, writable=True
        )
        if platform is Platform.SWITCH:
            # No aux-command switch entity class yet; leave to service.
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
        if cmd.id in skipped:
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
            # ERR is an Insteon PLM-side counter; gate on presence so
            # non-Insteon roots don't sprout a perpetual "Unavailable".
            if PROP_COMMS_ERROR in node.properties:
                isy_data.aux_properties[Platform.SENSOR].append(
                    (node, PROP_COMMS_ERROR)
                )
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

        # Non-Insteon native nodes (Z-Wave / Zigbee / Matter): the
        # dynamically-loaded nodedef drives the controllable platform via
        # the classifier — the type-based introspection below only knows
        # Insteon device classes and would default everything else
        # (energy meters, scene-controller buttons, …) to SWITCH. Fall
        # back to introspection only when it *positively* names a device
        # class the classifier's command-only view can't see (e.g. Z-Wave
        # locks speak LOCK/UNLOCK, not the Insteon SECMD verb).
        if node.protocol != Protocol.INSTEON:
            native_platform: Platform | None = None
            if result is not None and result.controllable is not None:
                native_platform = _CONTROLLABLE_TO_HA_PLATFORM.get(
                    result.controllable, Platform.SENSOR
                )
            elif node.is_thermostat:
                native_platform = Platform.CLIMATE
            elif node.is_lock:
                native_platform = Platform.LOCK
            elif node.is_fan:
                native_platform = Platform.FAN
            elif node.is_dimmable:
                native_platform = Platform.LIGHT
            elif result is None:
                # Nodedef not loaded yet and introspection found nothing —
                # keep the historical SWITCH default rather than dropping it.
                native_platform = Platform.SWITCH
            if native_platform is not None:
                isy_data.nodes[native_platform].append(node)
            if result is not None:
                _fan_out_readings(isy_data, node, result.readings)
                if _is_device_root(node):
                    _fan_out_commands(isy_data, node, controller, result)
            # EVENT entity registration is restricted to nodes with **no
            # primary platform** — i.e. true scene-controller / paddle
            # nodes (Z-Wave central scene endpoints, ``UZW0010``-shaped
            # nodedefs whose accepts is just ``QUERY`` but whose sends
            # list carries the press verbs). Switch / dimmer / lock /
            # fan / climate endpoints are skipped on purpose: eisy's
            # Z-Wave bridge isn't a reliable EVENT surface for those —
            # it echoes spurious DON/DOF on paired endpoints (a ZEN30
            # relay toggle fires DON/DOF on the dimmer side too) and
            # other plug nodedefs declare ``sends=[DON,DOF]`` but only
            # ever emit ``ST`` on the wire, so the event entity would
            # either fire phantom presses or never fire at all. Insteon
            # natives keep the broader rule below — their wire events
            # are faithful.
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS and native_platform is None:
                _register_event_node(isy_data, node, _node_trigger_commands(node))
            continue

        # Native Insteon nodes: classifier-driven primary with
        # type-introspection-first overrides for thermostat/lock/fan.
        primary = _primary_platform_for_native(node, result)

        # No primary platform: nodedef has no controllable surface at
        # all — accepts list is just bookkeeping verbs like
        # ``QUERY`` / ``BL`` (backlight) / ``WDU`` (write delta).
        # Examples: ``KeypadButton_ADV`` (KeypadLinc Dimmer LED-only
        # sub-buttons), ``RemoteLinc2_ADV`` (battery scene remotes),
        # ``IMETER_SOLO`` (energy meters), ``PIR2844`` (motion-only
        # sensors). Synthesising a primary entity would yield a
        # broken switch / light (DON not accepted on the wire), so
        # skip it and route the node onto EVENT if it sends verbs.
        #
        # Different shape: ``RelayLampSwitch_ADV`` / ``KeypadRelay_ADV``
        # / ``RelayLampSwitchLED_ADV`` / etc. *do* accept DON/DOF
        # (classifier returns SWITCH) but they're sub-buttons on a
        # multi-button physical paddle whose LED is *scene-controlled*
        # on real Insteon hardware — pointing DON directly at the
        # sub-button address doesn't reliably toggle the LED
        # (the controller drives it via the parent scene's
        # member-status logic). A direct switch entity would mislead
        # the user, so suppress the SWITCH primary too. Sub-address
        # dimmers (``DimmerLampSwitch_ADV`` paddles that genuinely
        # control a load) classify as LIGHT and fall through — those
        # are real load surfaces, not buttons.
        is_subnode_button = (
            primary == Platform.SWITCH
            and node.primary_address is not None
            and node.protocol == Protocol.INSTEON
        )
        if primary is None or is_subnode_button:
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


def _categorize_program_devices(
    isy_data: IsyData, programs: dict[str, Program], program_prefix: str
) -> None:
    """Collect every program *outside* the legacy switch convention.

    The ``HA.<platform>/<name>/{status,actions}`` folder layout is the
    pyisy 3.x "virtual device" pattern (see Home Assistant's `isy994`
    docs). Programs that follow it are already covered by the
    platform-specific surfaces in :func:`_categorize_programs` — we
    leave those untouched. Every *other* program (manually written
    automation, scheduler, scene helper, …) was previously invisible to
    HA; this collection drives the rich per-program device fan-out.
    """
    legacy_prefixes = tuple(
        f"{program_prefix}{platform}/" for platform in PROGRAM_PLATFORMS
    )
    for program in programs.values():
        path = program.path or ""
        if any(path.startswith(prefix) for prefix in legacy_prefixes):
            continue
        isy_data.program_devices.append(program)


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
