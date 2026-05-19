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
    AuxPlatform,
    ClassificationResult,
    ControllablePlatform,
    Controller,
    Node,
    Program,
    Variable,
    classify,
)
from pyisyox.constants import (
    CMD_BEEP,
    PROP_BUSY,
    PROP_COMMS_ERROR,
    TAG_ENABLED,
    Protocol,
)
from pyisyox.schema.nodedef import Command

from .const import (
    _LOGGER,
    BINARY_SENSOR_DEVICE_TYPES_ZWAVE,
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
from .editor_classification import BINARY_UOMS
from .entity import _pnode_group_naming
from .models import IsyData

#: Aux controls the consumer drops as a matter of HA *policy* (not
#: capability — controllable-ownership / coalescing is single-sourced in
#: ``pyisyox.classify``). ``BUSY`` is transient noise; ``COMMS_ERROR``
#: already has a dedicated root-scaffold sensor (see ``_categorize_nodes``).
_CONSUMER_SKIP_CONTROLS = frozenset({PROP_BUSY, PROP_COMMS_ERROR})


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

#: Map a coalesced aux-control's candidate platform to the HA platform
#: enum. The candidate is advisory — consumer overrides
#: (dedicated/service-only) and the per-device dedup sit on top.
_AUX_TO_HA_PLATFORM: dict[AuxPlatform, Platform] = {
    AuxPlatform.SENSOR: Platform.SENSOR,
    AuxPlatform.BINARY_SENSOR: Platform.BINARY_SENSOR,
    AuxPlatform.NUMBER: Platform.NUMBER,
    AuxPlatform.SELECT: Platform.SELECT,
    AuxPlatform.SWITCH: Platform.SWITCH,
    AuxPlatform.BUTTON: Platform.BUTTON,
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


def _is_native_binary_sensor(node: Node) -> bool:
    """True for native nodes that should land on the BINARY_SENSOR
    platform: Insteon ``BinaryAlarm`` variants and Z-Wave nodes whose
    generic-class category matches a known sensor family.

    Insteon: gates on the nodedef id (``BinaryAlarm`` /
    ``BinaryAlarm_ADV``) because the *capability* — "this node is a
    stateful binary sensor surface" — lives in the nodedef. Sub-nodes
    inherit the parent's nodedef id, so heartbeat / dusk-dawn / tamper /
    cool / heat sub-addresses all return True and the binary_sensor
    platform's setup folds them into the right partner entity.

    Z-Wave: gates on ``node.zwave_props.category`` against the
    ``BINARY_SENSOR_DEVICE_TYPES_ZWAVE`` table — the same lookup
    ``binary_sensor._detect_device_type_and_class`` uses for
    device_class assignment, so anything routed here will pick up its
    matching device class.

    The classifier can't make either decision on its own: BinaryAlarm
    nodedefs accept only ``QUERY`` (``controllable`` is always
    ``None``) and Z-Wave generic-class metadata isn't in the nodedef
    at all. The type triple drives the *device_class* sort downstream
    (moisture vs. opening vs. motion vs. cool/heat) where the
    leak/door/motion families look identical at the cmds level and
    the type byte is the only discriminator.
    """
    if node.protocol == Protocol.INSTEON:
        # Defensive: nodedef_id can be empty for nodes the controller
        # hasn't fully provisioned yet (joined-after-load race).
        return bool(node.nodedef_id and node.nodedef_id.startswith("BinaryAlarm"))
    if node.protocol == Protocol.ZWAVE and node.zwave_props is not None:
        category = node.zwave_props.category
        return any(
            category in values for values in BINARY_SENSOR_DEVICE_TYPES_ZWAVE.values()
        )
    return False


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
    """Run pyisyox's classifier; ``None`` when the nodedef isn't in the
    loaded profile (joined-after-load or unknown id)."""
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


def _fan_out_aux(isy_data: IsyData, node: Node, result: ClassificationResult) -> None:
    """Render ``result.aux_controls`` onto HA aux platforms.

    ``pyisyox.classify`` owns the capability split (controllable
    ownership, ``QUERY`` exclusion, read/write coalescing). This applies
    only consumer policy: protocol-skips for bespoke-entity (Insteon
    ``BEEP``) / service (Z-Wave ``CONFIG``) controls, the
    ``{BUSY, COMMS_ERROR}`` drop, and the ``NODE_AUX_FILTERS`` fallback
    when no editor resolved a candidate.
    """
    protocol_key = node.protocol or ""
    dedicated = _DEDICATED_COMMANDS_BY_PROTOCOL.get(protocol_key, frozenset())
    service_only = _SERVICE_ONLY_COMMANDS_BY_PROTOCOL.get(protocol_key, frozenset())
    for ac in result.aux_controls:
        if (
            ac.id in _CONSUMER_SKIP_CONTROLS
            or ac.id in dedicated
            or ac.id in service_only
        ):
            continue
        platform = (
            _AUX_TO_HA_PLATFORM.get(ac.candidate_platform)
            if ac.candidate_platform is not None
            else None
        )
        if platform is None:
            # Editor didn't resolve a candidate — fall back to the static
            # control→platform map, else leave it to the service.
            platform = NODE_AUX_FILTERS.get(ac.id)
        if platform is None:
            _LOGGER.debug(
                "No platform for aux control %s/%s; left to the service",
                node.address,
                ac.id,
            )
            continue
        isy_data.aux_properties[platform].append((node, ac.id))


def _dedupe_device_aux(isy_data: IsyData) -> None:
    """Collapse aux entities duplicated across one HA device's nodes.

    Sub-nodes fold onto the primary's device, so a control every
    sub-node reports (a KeypadLinc's ``BL``/``WDU``) would emit N
    identical entities. Dedup per ``(HA-device, control)``: own-device
    node wins, else the first sub-node — so a sub-node-only control (i3
    ``*Flags`` ``GVx``) still surfaces once, attributed to that sub-node.
    """
    for platform, items in isy_data.aux_properties.items():
        kept: dict[tuple[str, str], tuple[Node, str]] = {}
        for node, control in items:
            device = (
                node.address
                if _has_own_device(node)
                else (node.primary_address or node.address)
            )
            key = (device, control)
            current = kept.get(key)
            if current is None or (
                _has_own_device(node) and not _has_own_device(current[0])
            ):
                kept[key] = (node, control)
        isy_data.aux_properties[platform] = list(kept.values())


def _suggested_area_for_node(controller: Controller, node: Node) -> str | None:
    """Walk ``parent_address`` until a folder is hit; mirrors HA core
    ``isy994``'s ``node.folder``-as-``suggested_area``. Climbs through
    node ancestors so a sub-button on a primary inside a folder still
    resolves. ``None`` for root-level nodes."""
    # ``visited`` guards against a malformed parent-address cycle.
    visited: set[str] = set()
    address = node.parent_address
    while address and address not in visited:
        visited.add(address)
        folder = controller.folders.get(address)
        if folder is not None:
            return folder.name or None
        parent_node = controller.nodes.get(address)
        if parent_node is None:
            return None
        address = parent_node.parent_address
    return None


def _generate_device_info(
    controller: Controller, node: Node, host: str, sensor_string: str = ""
) -> DeviceInfo:
    """Generate the device info for a node that gets its own HA device.

    The device name is the pnode group's shared prefix
    (:func:`_pnode_group_naming`) — ``"… Leak"`` for a leak sensor whose
    primary node is ``"… Leak.Dry"`` — not the primary's raw label.

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
        name=_pnode_group_naming(controller.nodes, node, sensor_string)[0],
        via_device=via_device,
        configuration_url=host,
        suggested_area=_suggested_area_for_node(controller, node),
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
        # User-forced sensor classification: a marked scene becomes a
        # read-only binary_sensor instead of the default switch. Mirrors
        # the node short-circuit below (hacs-udi-iox#84).
        if sensor_identifier in group.name:
            isy_data.group_sensors.append(group)
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
                controller, node, host, sensor_identifier
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
            _fan_out_aux(isy_data, node, result)
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
            # Z-Wave sensor families (smoke, gas, leak, motion, …) win
            # over the classifier — pure sensors carry no controllable
            # surface, so the rich subnode-aware path in binary_sensor.py
            # needs the node on ``isy_data.nodes[BINARY_SENSOR]``.
            #
            # Z-Wave per-property readings (battery, temperature, …)
            # land on subnodes; ``_fan_out_aux`` + the per-device dedup
            # handle that.
            if _is_native_binary_sensor(node):
                isy_data.nodes[Platform.BINARY_SENSOR].append(node)
                if result is not None:
                    _fan_out_aux(isy_data, node, result)
                continue
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
                _fan_out_aux(isy_data, node, result)
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
        # type-introspection-first overrides for thermostat/lock/fan/
        # binary-sensor. Insteon binary sensors (leak, motion, opening)
        # carry no controllable surface, so without this override the
        # node falls through to EVENT-only and never surfaces stateful.
        if _is_native_binary_sensor(node):
            isy_data.nodes[Platform.BINARY_SENSOR].append(node)
            if result is not None:
                _fan_out_aux(isy_data, node, result)
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                _register_event_node(isy_data, node, _node_trigger_commands(node))
            continue

        primary = _primary_platform_for_native(node, result)

        # No controllable surface (KeypadButton_ADV, RemoteLinc2_ADV,
        # IMETER_SOLO, PIR2844 — accepts is just QUERY/BL/WDU). Skip
        # the primary entity and route to EVENT if the node sends
        # verbs. Sub-address nodedefs that *do* accept DON/DOF
        # (RelayLampSwitch_ADV, KeypadRelay_ADV, RelayLampSwitchLED_ADV)
        # are real load controllers — trust the nodedef and surface them
        # as switches.
        if primary is None:
            # No primary entity (KeypadButton_ADV, RemoteLinc2_ADV,
            # i3 ``*Flags`` …) — but the node may still carry aux
            # controls (i3 flag setters); surface them.
            if result is not None:
                _fan_out_aux(isy_data, node, result)
            if Platform.EVENT in NODE_PARALLEL_PLATFORMS:
                _register_event_node(isy_data, node, _node_trigger_commands(node))
            continue

        isy_data.nodes[primary].append(node)
        # Per-node fan-out; ``_dedupe_device_aux`` collapses per-device
        # duplicates afterward.
        if result is not None:
            _fan_out_aux(isy_data, node, result)

        # Parallel: native Insteon LIGHT/SWITCH nodes whose nodedef
        # declares sent verbs also feed the EVENT platform.
        if (
            Platform.EVENT in NODE_PARALLEL_PLATFORMS
            and primary in (Platform.LIGHT, Platform.SWITCH)
            and node.protocol == Protocol.INSTEON
        ):
            _register_event_node(isy_data, node, _node_trigger_commands(node))

    # Collapse per-device aux duplicates (see ``_dedupe_device_aux``).
    _dedupe_device_aux(isy_data)


def _categorize_programs(
    isy_data: IsyData, programs: dict[str, Program], ignore_identifier: str = ""
) -> None:
    """Walk the ``HA.<platform>/<name>/<status|actions>`` convention,
    pairing status/actions programs by inner name.

    Programs whose name — or any containing folder, since ``path`` is
    the slash-joined folder/name chain — contains ``ignore_identifier``
    are skipped, mirroring the node/group loop so the Ignore String
    option works controller-side for programs too (#62).
    """
    by_path: dict[str, Program] = {
        p.path: p
        for p in programs.values()
        if p.path
        and not (
            ignore_identifier
            and (ignore_identifier in p.path or ignore_identifier in (p.name or ""))
        )
    }

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
    isy_data: IsyData,
    programs: dict[str, Program],
    program_prefix: str,
    ignore_identifier: str = "",
) -> None:
    """Programs outside the legacy ``HA.<platform>/<name>/{status,actions}``
    convention — those are already platform-routed by
    :func:`_categorize_programs`. Everything else gets a per-program
    device fan-out.

    A program whose name or any containing folder (``path`` is the
    slash-joined folder/name chain) contains ``ignore_identifier`` is
    skipped, so the Ignore String option suppresses program devices
    controller-side (#62).
    """
    legacy_prefixes = tuple(
        f"{program_prefix}{platform}/" for platform in PROGRAM_PLATFORMS
    )
    for program in programs.values():
        path = program.path or ""
        if ignore_identifier and (
            ignore_identifier in path or ignore_identifier in (program.name or "")
        ):
            continue
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
