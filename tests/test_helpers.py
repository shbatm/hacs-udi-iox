"""Tests for helpers._categorize_nodes — the type-based + classifier
two-tier platform router.

Pins behavior that's invisible from a single function read:
- which native node attributes drive which HA platform
- how the sub-button suppression rule kicks in vs. doesn't
- aux-property fan-out (binary UOMs vs. sensor)
- plugin nodedefs falling through to pyisyox.classify

The tests build real :class:`pyisyox.Node` instances via
:mod:`pyisyox.testing` — introspection (``is_thermostat`` / ``is_lock`` /
``is_dimmable`` / ``is_fan``) flows through the real nodedef + editor
codec from the bundled anonymized eisy6 profile, so a pyisyox API change
fails these tests instead of drifting silently.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import Platform
from pyisyox import Node
from pyisyox.client import NodePropertyValue
from pyisyox.testing import (
    make_classified_node_record,
    make_controller,
    make_load_result,
    make_node,
    make_node_record,
    make_program,
    make_program_record,
)

from custom_components.udi_iox.helpers import (
    _categorize_nodes,
    _categorize_program_devices,
    _categorize_programs,
)
from custom_components.udi_iox.models import IsyData


@pytest.fixture
def isy_data():
    return IsyData()


@pytest.fixture
def options():
    return MappingProxyType({})


@pytest.fixture
def controller():
    return make_controller(make_load_result())


def _node(
    controller, address: str, *, target: str | None = None, **kwargs: Any
) -> Node:
    """Build a real Node from a record. ``target`` picks a nodedef id
    that resolves to the requested platform via the bundled profile."""
    name = kwargs.pop("name", "Test")
    if target is not None:
        record = make_classified_node_record(address, name, target=target, **kwargs)
    else:
        record = make_node_record(address, name, **kwargs)
    return make_node(record, controller)


def _categorize(
    isy_data: IsyData,
    node: Node,
    options: MappingProxyType[str, Any],
    *,
    controller: Any,
    host: str = "https://eisy.local:443",
) -> None:
    nodes = {node.address: node}
    _categorize_nodes(isy_data, nodes, options, controller=controller, host=host)


# --- primary platform routing -----------------------------------------


def test_thermostat_classifies_as_climate(isy_data, options, controller):
    node = _node(controller, "A 1", target="climate")
    _categorize(isy_data, node, options, controller=controller)
    assert isy_data.nodes[Platform.CLIMATE] == [node]


def test_lock_classifies_as_lock(isy_data, options, controller):
    node = _node(controller, "A 1", target="lock")
    _categorize(isy_data, node, options, controller=controller)
    assert isy_data.nodes[Platform.LOCK] == [node]


def test_dimmable_classifies_as_light(isy_data, options, controller):
    node = _node(controller, "A 1", target="light")
    _categorize(isy_data, node, options, controller=controller)
    assert isy_data.nodes[Platform.LIGHT] == [node]


def test_fan_classifies_as_fan(isy_data, options, controller):
    """FanLincMotor surfaces as ``Platform.FAN`` rather than falling
    through to SWITCH (its current behaviour without ``is_fan``)."""
    node = _node(controller, "A 2", target="fan")
    _categorize(isy_data, node, options, controller=controller)
    assert isy_data.nodes[Platform.FAN] == [node]


def test_fan_takes_precedence_over_dimmable(isy_data, options, controller):
    """A node reporting both ``is_fan`` and ``is_dimmable`` (e.g. a
    plugin fan with a multilevel ST editor) classifies as FAN, not
    LIGHT.

    No profile nodedef satisfies both naturally — patch ``is_dimmable``
    on the FanLincMotor-resolved Node so the test exercises the
    consumer's ``_primary_platform_for_native`` ordering directly.
    """
    node = _node(controller, "A 3", target="fan")
    with patch.object(
        Node, "is_dimmable", new_callable=lambda: property(lambda _self: True)
    ):
        _categorize(isy_data, node, options, controller=controller)
    assert isy_data.nodes[Platform.FAN] == [node]
    assert isy_data.nodes[Platform.LIGHT] == []


def test_default_native_classifies_as_switch(isy_data, options, controller):
    node = _node(controller, "A 1", target="switch")
    _categorize(isy_data, node, options, controller=controller)
    assert isy_data.nodes[Platform.SWITCH] == [node]


# --- sub-button suppression -------------------------------------------


def test_keypadbutton_subbutton_routes_to_event_only(isy_data, options, controller):
    """A KeypadLinc Dimmer LED-only sub-button (``KeypadButton_ADV``)
    has accepts = QUERY/BL/WDU — no DON. The classifier returns no
    controllable platform, so the consumer routes it to EVENT only,
    never as a switch / light. This is the "no controllable surface"
    suppression path."""
    node = _node(
        controller, "AA BB CC 2", nodedef_id="KeypadButton_ADV", pnode="AA BB CC 1"
    )
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == []
    assert isy_data.nodes[Platform.LIGHT] == []
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_subbutton_relay_paddle_keeps_switch_classification(
    isy_data, options, controller
):
    """A 2477S On/Off Switch sub-node (``RelayLampSwitch_ADV``) IS a
    real load controller — accepts DON/DOF — and must surface as
    SWITCH + EVENT, not be suppressed. Trust the nodedef rather than
    second-guessing whether ``primary_address is not None`` means
    "sub-button" — the only thing that matters is whether the device
    actually accepts on/off commands."""
    node = _node(
        controller,
        "AA BB CC 2",
        nodedef_id="RelayLampSwitch_ADV",
        pnode="AA BB CC 1",
    )
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_subbutton_dimmer_paddle_keeps_light_classification(
    isy_data, options, controller
):
    """A dimmable sub-node (``DimmerLampSwitch_ADV``) is a real load
    surface — keep primary=LIGHT + parallel=EVENT."""
    node = _node(controller, "AA BB CC 2", target="subdimmer", pnode="AA BB CC 1")
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.LIGHT] == [node]
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_root_load_keeps_switch_classification(isy_data, options, controller):
    """Root SWITCH-shape Insteon node whose nodedef declares sent verbs
    → SWITCH + EVENT, NOT suppressed. ``KeypadRelay`` sends the seven
    press/fast/fade verbs."""
    node = _node(controller, "AA BB CC 1", nodedef_id="KeypadRelay")
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    assert isy_data.nodes[Platform.EVENT] == [node]
    assert isy_data.node_triggers["AA BB CC 1"]


def test_load_without_sent_verbs_skips_event(isy_data, options, controller):
    """A node whose nodedef declares no ``cmds.sends`` (e.g. the
    ``RelayLampOnly`` load node) gets its primary entity but no EVENT
    entity — there's nothing for it to fire."""
    node = _node(controller, "AA BB CC 1", target="switch")
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    assert isy_data.nodes[Platform.EVENT] == []
    assert "AA BB CC 1" not in isy_data.node_triggers


def test_insteon_leak_sensor_routes_to_binary_sensor_and_event(
    isy_data, options, controller
):
    """Insteon binary sensors land on BINARY_SENSOR via the type-override
    path; EVENT registers in parallel for sub-button transitions."""
    from pyisyox.testing import make_leak_sensor_records, make_motion_sensor_records

    records = {**make_leak_sensor_records(), **make_motion_sensor_records()}
    nodes = {addr: make_node(rec, controller) for addr, rec in records.items()}
    _categorize_nodes(isy_data, nodes, options, controller=controller, host="https://h")

    bs_addrs = {n.address for n in isy_data.nodes[Platform.BINARY_SENSOR]}
    # Every leak / motion sub-node is on BINARY_SENSOR (parents + heartbeat
    # + dusk-dawn + tamper + low-battery + disabled subnode).
    assert "30 30 30 1" in bs_addrs and "30 30 30 4" in bs_addrs  # leak primary + hb
    assert "32 32 32 1" in bs_addrs  # motion primary
    assert "32 32 32 2" in bs_addrs  # motion dusk/dawn
    assert "32 32 32 3" in bs_addrs  # motion low-battery

    # And EVENT still fires alongside.
    event_addrs = {n.address for n in isy_data.nodes[Platform.EVENT]}
    assert "30 30 30 1" in event_addrs
    assert "32 32 32 1" in event_addrs


def test_zwave_motion_sensor_routes_to_binary_sensor(isy_data, options, controller):
    """Z-Wave nodes whose ``zwave_props.category`` matches a known
    sensor family in ``BINARY_SENSOR_DEVICE_TYPES_ZWAVE`` route to
    BINARY_SENSOR."""
    from pyisyox.client import ZWaveProperties

    rec = make_node_record(
        "ZW019_1", "ZW Motion", family_id="4", nodedef_id="UZW0099", type_="4.16.1.0"
    )
    # category "155" is the Z-Wave Notification (motion) generic class.
    rec.zwave_props = ZWaveProperties.from_devtype(
        {"cat": "155", "mfg": "634.257.13", "gen": "4.16.1"}
    )
    node = make_node(rec, controller)

    _categorize(isy_data, node, options, controller=controller)
    assert node in isy_data.nodes[Platform.BINARY_SENSOR]


def test_zwave_node_without_devtype_does_not_route_to_binary_sensor(
    isy_data, options, controller
):
    """A Z-Wave node that doesn't expose a ``devtype`` block (or whose
    category isn't a known sensor family) must NOT land on
    BINARY_SENSOR — falls through to the regular Z-Wave classifier
    path. Pins the gate's null-safety."""
    rec = make_node_record(
        "ZW020_1", "ZW Switch", family_id="4", nodedef_id="UZW000F", type_="4.17.1.0"
    )
    # rec.zwave_props stays None — no devtype block published.
    node = make_node(rec, controller)

    _categorize(isy_data, node, options, controller=controller)
    assert node not in isy_data.nodes[Platform.BINARY_SENSOR]


def test_remotelinc_button_no_primary_entity_routes_to_event_only(
    isy_data, options, controller
):
    """``RemoteLinc2_ADV`` is a battery-powered scene-button sub-node:
    nodedef accepts only ``WDU`` so ``classify().controllable is None``.
    Synthesising a primary entity would yield a broken switch / light
    (DON would not be accepted), so the node must end up EVENT-only.
    Regression for the user-reported bug where a paired RemoteLinc2
    surfaced as 8 broken light entities."""
    node = _node(
        controller,
        "3E FF 1F 7",
        nodedef_id="RemoteLinc2_ADV",
        pnode="3E FF 1F 1",
    )
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.LIGHT] == []
    assert isy_data.nodes[Platform.SWITCH] == []
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_subbutton_non_insteon_not_suppressed(isy_data, options, controller):
    """The sub-button rule is Insteon-specific; Z-Wave sub-nodes (rare
    but real on multi-endpoint devices) shouldn't be silently dropped.

    Family ``"4"`` is Z-Wave — overrides the default Insteon family
    on the relay nodedef so ``Node.protocol`` resolves to ``"zwave"``.
    """
    node = _node(
        controller,
        "ZW 2",
        target="switch",
        family_id="4",
        pnode="ZW 1",
    )
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    # The non-Insteon path registers EVENT from the nodedef's ``cmds.sends``;
    # ``RelayLampOnly`` declares none, so this sub-node stays switch-only.
    assert isy_data.nodes[Platform.EVENT] == []


def test_zwave_node_with_no_controllable_is_not_a_switch(isy_data, options, controller):
    """A Z-Wave node whose (dynamically-loaded) nodedef has no DON/DOF
    accept and isn't a recognised device class must NOT fall into the
    Insteon SWITCH default — it gets only its readings (an energy meter:
    ST/TPW sensors). A Central-Scene controller (accepts QUERY only,
    *sends* the press verbs) gets an EVENT entity, no switch."""
    from pyisyox.client import NodePropertyValue
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty

    energy_def = NodeDef(
        id="UZW0016",
        family_id="4",
        instance_id="1",
        properties={
            "ST": NodeProperty(id="ST", editor_id=""),
            "TPW": NodeProperty(id="TPW", editor_id=""),
        },
        cmds=NodeCommands(accepts=[Command(id="QUERY"), Command(id="RESET")]),
    )
    rec = make_node_record(
        "ZW003_143",
        "Energy Meter",
        family_id="4",
        nodedef_id="UZW0016",
        properties={
            "ST": NodePropertyValue(id="ST", value="0"),
            "TPW": NodePropertyValue(id="TPW", value="0"),
        },
    )
    node = make_node(rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: energy_def)
    ):
        _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == []
    assert isy_data.nodes[Platform.LIGHT] == []
    sensor_owners = {n for n, _ in isy_data.aux_properties[Platform.SENSOR]}
    assert node in sensor_owners

    # Central-Scene controller shape: accepts QUERY only, sends the verbs.
    scene_def = NodeDef(
        id="UZW0010",
        family_id="4",
        instance_id="1",
        cmds=NodeCommands(
            accepts=[Command(id="QUERY")],
            sends=[Command(id="DON3"), Command(id="DON4"), Command(id="FDUP")],
        ),
    )
    scene_data = IsyData()
    scene_rec = make_node_record(
        "ZW002_201", "Scene Button", family_id="4", nodedef_id="UZW0010"
    )
    scene_node = make_node(scene_rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: scene_def)
    ):
        _categorize(scene_data, scene_node, options, controller=controller)

    assert scene_data.nodes[Platform.SWITCH] == []
    assert scene_data.nodes[Platform.EVENT] == [scene_node]


# Encoded editor id (UOM 51, 0-100 %) — decodes scope-free via
# Editor.from_encoded_id, so resolve_editor() resolves it without
# grafting an editor into the plugin family. → Platform.NUMBER.
_ENCODED_PCT_EDITOR = "_51_0_R_0_101_N_IX_DIM_REP"


def test_plugin_init_st_setter_surfaces_when_uncontrollable(
    isy_data, options, controller
):
    """``param.init`` is a UI seed-source (which property pre-fills the
    input), NOT a "the primary owns this" signal — IoX nodedefs declare
    no command→property writeback map. ``classify`` already excludes the
    primary's verbs from ``parameterized_commands``, so a setter that
    lands here is by construction not primary-owned and must surface.

    A node-server nodedef whose only state surface is a setter seeded
    from ``ST`` (and which classifies with no controllable primary) must
    surface that setter — pre-#64 it was dropped on ``init == "ST"``,
    leaving the node with nothing to control.
    """
    from pyisyox.schema.cmd import Command, CommandParameter
    from pyisyox.schema.nodedef import NodeCommands, NodeDef

    setter_def = NodeDef(
        id="VirtualStatusSetter",
        family_id="199",  # non-core family → Protocol.NODE_SERVER
        instance_id="1",
        cmds=NodeCommands(
            accepts=[
                Command(
                    id="SETST",
                    name="Set Status",
                    parameters=[
                        CommandParameter(editor_id=_ENCODED_PCT_EDITOR, init="ST")
                    ],
                )
            ]
        ),
    )
    rec = make_node_record(
        "n199_1",
        "Virtual Generic",
        nodedef_id="VirtualStatusSetter",
        family_id="199",
        instance_id="1",
    )
    node = make_node(rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: setter_def)
    ):
        _categorize(isy_data, node, options, controller=controller)

    assert (node, "SETST") in isy_data.aux_properties[Platform.NUMBER]


def test_virtualgeneric_becomes_switch_with_level_setter_aux(
    isy_data, options, controller
):
    """End-to-end on the real ``virtualgeneric`` shape (#64 / Virtual#11)
    against pyisyox ≥ 6.0.0b6: parameterless ``DON``/``DOF`` + ``BRT``/
    ``DIM`` + multilevel ``ST``/``OL`` + ``SETST``(init=ST) / ``SETOL``
    (init=OL).

    Prong 2 (pyisyox b5): a parameterless ``DON`` is not HA-dimmable, so
    ``classify`` returns SWITCH (not LIGHT) and claims only ``DON``/
    ``DOF``. Aux coalescing (#160 / hacs#67): ``SETST``/``SETOL`` are not
    the primary's verbs, so they survive — but each is now folded with
    the status its parameter's ``init`` names (``SETST`` ⇄ ``ST``,
    ``SETOL`` ⇄ ``OL``), so the NUMBER aux controls are keyed by the
    *status* id (read + write in one entity, named "Status" / "On
    Level"), not the raw command id. Net: a working switch plus two
    read/write level numbers, instead of a broken brightness slider.
    """
    from pyisyox.schema.cmd import Command, CommandParameter
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty

    vgeneric = NodeDef(
        id="virtualgeneric",
        family_id="199",
        instance_id="1",
        properties={
            "ST": NodeProperty(id="ST", editor_id=_ENCODED_PCT_EDITOR),
            "OL": NodeProperty(id="OL", editor_id=_ENCODED_PCT_EDITOR),
        },
        cmds=NodeCommands(
            accepts=[
                Command(id="DON", name="On"),  # parameterless — not dimmable
                Command(id="DOF", name="Off"),
                Command(id="BRT", name="Brighten"),
                Command(id="DIM", name="Dim"),
                Command(
                    id="SETST",
                    name="Set Status",
                    parameters=[
                        CommandParameter(editor_id=_ENCODED_PCT_EDITOR, init="ST")
                    ],
                ),
                Command(
                    id="SETOL",
                    name="Set On Level",
                    parameters=[
                        CommandParameter(editor_id=_ENCODED_PCT_EDITOR, init="OL")
                    ],
                ),
            ]
        ),
    )
    rec = make_node_record(
        "n199_3",
        "Virtual Generic",
        nodedef_id="virtualgeneric",
        family_id="199",
        instance_id="1",
    )
    node = make_node(rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: vgeneric)
    ):
        _categorize(isy_data, node, options, controller=controller)

    assert node in isy_data.nodes[Platform.SWITCH]
    assert node not in isy_data.nodes[Platform.LIGHT]
    number_aux = {
        cmd for n, cmd in isy_data.aux_properties[Platform.NUMBER] if n is node
    }
    # Coalesced onto the init-linked status ids, not the raw setter ids.
    assert {"ST", "OL"} <= number_aux
    assert {"SETST", "SETOL"}.isdisjoint(number_aux)


def test_zwave_switch_with_declared_sends_skips_event(isy_data, options, controller):
    """A Z-Wave binary-switch nodedef declares ``sends=[DON,DOF]`` but the
    device only ever emits ``ST`` on the wire (and eisy echoes spurious
    DON/DOF onto paired endpoints — a ZEN30 relay toggle fires DON/DOF
    on the dimmer side too). The EVENT entity for those is either dead
    or misleading, so we don't register one when the node already has a
    primary HA platform.
    """
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty

    switch_def = NodeDef(
        id="UZW000F",
        family_id="4",
        instance_id="1",
        properties={"ST": NodeProperty(id="ST", editor_id="ZW_OFF_ON_UNKNOWN")},
        cmds=NodeCommands(
            accepts=[Command(id="DON"), Command(id="DOF"), Command(id="QUERY")],
            sends=[Command(id="DON"), Command(id="DOF")],
        ),
    )
    rec = make_node_record("ZW003_1", "Smart Plug", family_id="4", nodedef_id="UZW000F")
    node = make_node(rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: switch_def)
    ):
        _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    assert isy_data.nodes[Platform.EVENT] == []
    assert "ZW003_1" not in isy_data.node_triggers


def test_zwave_config_command_is_not_fanned_out_as_aux(isy_data, options, controller):
    """Z-Wave ``CONFIG`` (the ``UZW*`` dimmer/switch nodedefs' parameter
    write surface) takes a ``(NUM, VAL)`` pair editor — but the
    parameter's *byte size* is a third device-defined arg that doesn't
    fit either field, so the auto-fan-out slider would drop it. The
    integration suppresses the entity entirely and routes parameter
    writes through ``udi_iox.set_zwave_parameter`` instead.
    """
    from pyisyox.schema.cmd import Command, CommandParameter
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty

    switch_def = NodeDef(
        id="UZW000F",
        family_id="4",
        instance_id="1",
        properties={"ST": NodeProperty(id="ST", editor_id="ZW_OFF_ON_UNKNOWN")},
        cmds=NodeCommands(
            accepts=[
                Command(id="DON"),
                Command(id="DOF"),
                Command(
                    id="CONFIG",
                    parameters=[
                        CommandParameter(editor_id="_107_0_R_0_255"),
                        CommandParameter(editor_id="ZW_CONFIG"),
                    ],
                ),
                Command(id="QUERY"),
            ],
            sends=[Command(id="DON"), Command(id="DOF")],
        ),
    )
    rec = make_node_record("ZW003_1", "Smart Plug", family_id="4", nodedef_id="UZW000F")
    node = make_node(rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: switch_def)
    ):
        _categorize(isy_data, node, options, controller=controller)

    config_aux_owners = {
        n
        for aux_list in isy_data.aux_properties.values()
        for n, control in aux_list
        if control == "CONFIG"
    }
    assert node not in config_aux_owners


def test_insteon_config_command_still_fans_out(isy_data, options, controller):
    """The suppression rule is Z-Wave-only — Insteon ``CONFIG`` (rare;
    legacy, single-byte) stays a slider so we don't regress anything
    Insteon users already relied on."""
    from pyisyox.schema.cmd import Command, CommandParameter
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty

    insteon_def = NodeDef(
        id="InsteonWithConfig",
        family_id="1",
        instance_id="1",
        properties={"ST": NodeProperty(id="ST", editor_id="I_BOOL")},
        cmds=NodeCommands(
            accepts=[
                Command(id="DON"),
                Command(id="DOF"),
                Command(
                    id="CONFIG",
                    parameters=[CommandParameter(editor_id="I_OL")],
                ),
            ],
        ),
    )
    rec = make_node_record(
        "1A 2B 3C 1", "Insteon", family_id="1", nodedef_id="InsteonWithConfig"
    )
    node = make_node(rec, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: insteon_def)
    ):
        _categorize(isy_data, node, options, controller=controller)

    config_aux_owners = {
        n
        for aux_list in isy_data.aux_properties.values()
        for n, control in aux_list
        if control == "CONFIG"
    }
    assert node in config_aux_owners


# --- aux property fan-out ---------------------------------------------


def test_keypaddimmer_backlight_routes_to_select(isy_data, options, controller):
    """KeypadDimmer_ADV's backlight editor is UOM_INDEX (discrete on/off
    pairs) — fan out to SELECT, not NUMBER."""
    node = _node(controller, "AA BB CC 1", nodedef_id="KeypadDimmer_ADV")
    _categorize(isy_data, node, options, controller=controller)
    select_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.SELECT]]
    number_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.NUMBER]]
    assert (node, "BL") in select_aux
    assert (node, "BL") not in number_aux


def test_dimmerlampswitch_backlight_routes_to_number(isy_data, options, controller):
    """DimmerLampSwitch_ADV's backlight editor is UOM_PERCENTAGE
    (continuous 0-100 intensity) — fan out to NUMBER, not SELECT."""
    node = _node(controller, "AA BB CC 1", nodedef_id="DimmerLampSwitch_ADV")
    _categorize(isy_data, node, options, controller=controller)
    select_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.SELECT]]
    number_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.NUMBER]]
    assert (node, "BL") in number_aux
    assert (node, "BL") not in select_aux


def test_unsupported_nodedef_skips_backlight(isy_data, options, controller):
    """Nodedefs absent from BACKLIGHT_SUPPORT (DoorLock, Thermostat,
    plugin nodes) don't get a backlight aux entity."""
    node = _node(controller, "CC CC CC 1", target="lock")
    _categorize(isy_data, node, options, controller=controller)
    select_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.SELECT]]
    number_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.NUMBER]]
    assert (node, "BL") not in select_aux
    assert (node, "BL") not in number_aux


def test_root_dimmer_fans_aux_props_to_number_select(isy_data, options, controller):
    """Root dimmable nodes spawn NUMBER (on_level) + SELECT
    (ramp_rate) entities for the matching aux properties."""
    record = make_classified_node_record(
        "AA BB CC 1",
        "Hallway Dimmer",
        target="light",
        properties={
            "ST": NodePropertyValue(id="ST", value="100", uom="51"),
            "OL": NodePropertyValue(id="OL", value="200", uom="100"),
            "RR": NodePropertyValue(id="RR", value="20", uom="25"),
        },
    )
    node = make_node(record, controller)
    _categorize(isy_data, node, options, controller=controller)

    on_level_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.NUMBER]]
    ramp_rate_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.SELECT]]
    assert (node, "OL") in on_level_aux
    assert (node, "RR") in ramp_rate_aux


def test_aux_property_with_binary_uom_routes_to_binary_sensor(
    isy_data, options, controller
):
    """A read-only aux *property* whose editor UOM is binary (2 / 78 /
    79) fans out to BINARY_SENSOR; a non-binary one to SENSOR.

    Nodedef-driven (#160 / hacs#67): the split is the classifier's
    editor-UOM read on the *nodedef* property, surfaced as the aux
    control's ``candidate_platform`` — not a walk of runtime-reported
    ``NodePropertyValue`` UOMs (a node never reports a property absent
    from its nodedef; ``/api/status`` is merged at load). The two GVx
    here are declared properties with encoded editor ids that resolve
    scope-free (UOM 2 = boolean → BINARY_SENSOR; UOM 51 = percent →
    SENSOR). ``DON``/``DOF`` make it a SWITCH so ST/OL/RR are
    controllable-owned and don't leak into the aux set.
    """
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeProperty

    mixed_def = NodeDef(
        id="MixedUOM",
        family_id="1",
        instance_id="1",
        properties={
            "ST": NodeProperty(id="ST", editor_id=_ENCODED_PCT_EDITOR),
            "GV1": NodeProperty(id="GV1", editor_id="_2_0"),  # UOM 2 → binary
            "GV2": NodeProperty(id="GV2", editor_id="_51_0"),  # UOM 51 → sensor
        },
        cmds=NodeCommands(
            accepts=[Command(id="DON", name="On"), Command(id="DOF", name="Off")]
        ),
    )
    record = make_classified_node_record(
        "AA BB CC 1", "Mixed-UOM Device", target="switch"
    )
    node = make_node(record, controller)
    with patch.object(
        Node, "nodedef", new_callable=lambda: property(lambda _self: mixed_def)
    ):
        _categorize(isy_data, node, options, controller=controller)

    assert (node, "GV1") in [
        (n, c) for n, c in isy_data.aux_properties[Platform.BINARY_SENSOR]
    ]
    assert (node, "GV2") in [
        (n, c) for n, c in isy_data.aux_properties[Platform.SENSOR]
    ]


# --- ignore / sensor identifier ---------------------------------------


def test_ignore_string_skips_node(isy_data, controller):
    """A node whose name contains the configured ignore string is
    excluded from every platform."""
    node = _node(controller, "A 1", target="switch", name="Hallway {IGNORE ME}")
    opts = MappingProxyType({"ignore_string": "{IGNORE ME}"})
    _categorize(isy_data, node, opts, controller=controller)

    for platform in (
        Platform.LIGHT,
        Platform.SWITCH,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.EVENT,
    ):
        assert isy_data.nodes[platform] == []


def test_sensor_identifier_forces_sensor_classification(isy_data, controller):
    """A node whose name contains the sensor identifier is forced to
    SENSOR, bypassing the type-based introspection. Useful for
    custom dimmable devices the user wants reported as a sensor."""
    record = make_classified_node_record(
        "A 1",
        "Outdoor Lux sensor",
        target="light",  # Would normally classify as LIGHT
        properties={"ST": NodePropertyValue(id="ST", value="500", uom="36")},
    )
    node = make_node(record, controller)
    opts = MappingProxyType({"sensor_string": "sensor"})
    _categorize(isy_data, node, opts, controller=controller)

    assert isy_data.nodes[Platform.SENSOR] == [node]
    assert isy_data.nodes[Platform.LIGHT] == []


def test_sensor_string_marker_stripped_from_device_name(isy_data, controller):
    """#80: the configured sensor_string is matched verbatim on the raw
    name for forced classification, but stripped from the HA device
    name so the marker never leaks into the UI / entity_id."""
    record = make_classified_node_record(
        "A 1",
        "Garbage Disposal {SENSOR}",
        target="switch",
        properties={"ST": NodePropertyValue(id="ST", value="0", uom="36")},
    )
    node = make_node(record, controller)
    _categorize(
        isy_data,
        node,
        MappingProxyType({"sensor_string": "{SENSOR}"}),
        controller=controller,
    )

    assert isy_data.nodes[Platform.SENSOR] == [node]  # classified on raw name
    assert isy_data.nodes[Platform.SWITCH] == []
    device_info = isy_data.devices.get(node.address)
    assert device_info is not None
    assert device_info["name"] == "Garbage Disposal"  # marker stripped


def test_default_sensor_string_does_not_match_bare_word_sensor(isy_data, controller):
    """#80: the bracketed default ``{SENSOR}`` must not false-match a
    node-server entity that legitimately contains the word "sensor"
    (the reason the default changed from the bare word)."""
    record = make_classified_node_record("A 1", "Flume Water Sensor", target="switch")
    node = make_node(record, controller)
    _categorize(isy_data, node, MappingProxyType({}), controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]  # NOT forced to sensor
    assert isy_data.nodes[Platform.SENSOR] == []
    device_info = isy_data.devices.get(node.address)
    assert device_info is not None
    assert device_info["name"] == "Flume Water Sensor"  # unchanged


# --- node-server device hierarchy ------------------------------------


def test_node_server_children_get_own_device_info(isy_data, options, controller):
    """A plugin controller + 2 plugin children should each receive
    their own :class:`DeviceInfo` rather than the children folding
    under the controller — matches the eisy UI per-sensor cards and
    avoids "Current"/"Leak Detected" name collisions when siblings
    share aux property labels.
    """
    from pyisyox.testing import (
        PLUGIN_COVER_FAMILY_ID,
        PLUGIN_COVER_INSTANCE_ID,
        PLUGIN_COVER_NODEDEF_ID,
    )

    from custom_components.udi_iox.const import DOMAIN

    controller_record = make_node_record(
        "n100_controller",
        "Plugin Hub",
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        type_="",
    )
    child_a = make_node_record(
        "n100_blind1",
        "Blind A",
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        pnode="n100_controller",
        type_="",
    )
    child_b = make_node_record(
        "n100_blind2",
        "Blind B",
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        pnode="n100_controller",
        type_="",
    )
    nodes = {
        rec.address: make_node(rec, controller)
        for rec in (controller_record, child_a, child_b)
    }
    _categorize_nodes(
        isy_data, nodes, options, controller=controller, host="https://eisy.local"
    )

    uuid = controller.config.uuid
    # All three nodes own their own DeviceInfo.
    assert set(isy_data.devices) == {"n100_controller", "n100_blind1", "n100_blind2"}
    assert isy_data.devices["n100_controller"]["identifiers"] == {
        (DOMAIN, f"{uuid}_n100_controller")
    }
    assert isy_data.devices["n100_blind1"]["identifiers"] == {
        (DOMAIN, f"{uuid}_n100_blind1")
    }
    # Children's via_device anchors on the controller node, not the
    # eisy root — gives HA the hub → child hierarchy.
    assert isy_data.devices["n100_blind1"]["via_device"] == (
        DOMAIN,
        f"{uuid}_n100_controller",
    )
    assert isy_data.devices["n100_blind2"]["via_device"] == (
        DOMAIN,
        f"{uuid}_n100_controller",
    )
    # Controller still anchors on the eisy root.
    assert isy_data.devices["n100_controller"]["via_device"] == (DOMAIN, uuid)


def test_node_server_controller_no_comms_error_sensor(isy_data, options, controller):
    """A plugin controller node has no ERR property on the wire — the
    integration must not synthesise a perpetually-Unavailable
    ``device_communication_errors`` sensor for it.
    """
    from pyisyox.testing import (
        PLUGIN_COVER_FAMILY_ID,
        PLUGIN_COVER_INSTANCE_ID,
        PLUGIN_COVER_NODEDEF_ID,
    )

    record = make_node_record(
        "n100_controller",
        "Plugin Hub",
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        type_="",
    )
    _categorize(isy_data, make_node(record, controller), options, controller=controller)

    sensor_aux = [c for _n, c in isy_data.aux_properties[Platform.SENSOR]]
    assert "ERR" not in sensor_aux


def test_native_insteon_root_keeps_comms_error_sensor(isy_data, options, controller):
    """An Insteon root carries ERR on the wire; the diagnostic sensor
    must still be created (regression guard for the new
    presence-gated path)."""
    node = _node(controller, "AA BB CC 1", target="switch")
    _categorize(isy_data, node, options, controller=controller)

    sensor_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.SENSOR]]
    assert (node, "ERR") in sensor_aux


# --- editor-driven aux command fan-out (issue #10) --------------------


def test_plugin_dimmer_aux_commands_classified_by_editor(isy_data, options):
    """A PG3 node's parameterised setters land on the HA platform their
    *editor* implies: a pure-enum editor (``names``, no numeric bounds)
    → SELECT; the generic ``INTEGER`` editor → NUMBER.

    The ``PluginDimmer`` testing fixture's ``DON`` carries an
    ``I_OL`` on-level param (pyisyox ≥ 6.0.0b8 / pyisyox#159), so the
    classifier correctly routes it to LIGHT — HA can drive brightness
    via ``DON <level>``. The editor-driven aux routing under test is
    independent of the primary platform.
    """
    from pyisyox.testing import (
        make_controller,
        make_dimmer_plugin_load_result,
        make_node,
        make_plugin_dimmer_node_record,
    )

    controller = make_controller(make_dimmer_plugin_load_result())
    record = make_plugin_dimmer_node_record("n103_lamp", "Studio Lamp")
    node = make_node(record, controller)
    _categorize_nodes(
        isy_data,
        {node.address: node},
        options,
        controller=controller,
        host="https://eisy.local",
    )

    assert node in isy_data.nodes[Platform.LIGHT]
    assert node not in isy_data.nodes[Platform.SWITCH]
    assert (node, "SETMODE") in isy_data.aux_properties[Platform.SELECT]
    assert (node, "THRESHOLD") in isy_data.aux_properties[Platform.NUMBER]
    # SETMODE's enum editor must not be mistaken for a slider, and the
    # INTEGER setter must not become a 1000-option dropdown.
    assert (node, "SETMODE") not in isy_data.aux_properties[Platform.NUMBER]
    assert (node, "THRESHOLD") not in isy_data.aux_properties[Platform.SELECT]
    # A bool-editor *command* with no ``init`` (write-only) resolves to
    # SWITCH and is now surfaced — ``ISYAuxControlSwitchEntity`` drives
    # it optimistically (hacs#67). It must land *only* on SWITCH.
    assert (node, "INVERT") in isy_data.aux_properties[Platform.SWITCH]
    for platform in (Platform.NUMBER, Platform.SELECT, Platform.BUTTON):
        assert (node, "INVERT") not in isy_data.aux_properties[platform]


# --- guard rail -------------------------------------------------------


def test_categorize_no_op_when_controller_is_none(isy_data, options, controller):
    """Defensive default — older test fixtures invoke
    _categorize_nodes without a controller; the function returns
    cleanly so the test isn't forced to mock the full surface."""
    node = _node(controller, "A 1", target="switch")
    _categorize_nodes(isy_data, {"A 1": node}, options, controller=None)
    assert isy_data.nodes[Platform.SWITCH] == []


# --- _categorize_programs --------------------------------------------


def test_categorize_programs_pairs_status_with_actions(isy_data, controller):
    """``HA.switch/Foo/status`` + ``HA.switch/Foo/actions`` programs
    pair up under ``Platform.SWITCH`` keyed by the inner name ``Foo``."""
    status = make_program(
        make_program_record(
            "0010", "status", path="HA.switch/Foo/status", status=False
        ),
        controller,
    )
    actions = make_program(
        make_program_record("0011", "actions", path="HA.switch/Foo/actions"),
        controller,
    )

    _categorize_programs(isy_data, {"0010": status, "0011": actions})

    assert isy_data.programs[Platform.SWITCH] == [("Foo", status, actions)]


def test_categorize_programs_warns_when_actions_missing(isy_data, controller, caplog):
    """For non-binary platforms a status program without a sibling
    actions program logs a warning but still loads — the entity will
    just have no working ``turn_on`` / ``turn_off`` path."""
    status = make_program(
        make_program_record(
            "0010", "status", path="HA.switch/Foo/status", status=False
        ),
        controller,
    )

    _categorize_programs(isy_data, {"0010": status})

    assert isy_data.programs[Platform.SWITCH] == [("Foo", status, None)]
    assert any("missing actions program" in m for m in caplog.messages)


def test_categorize_programs_binary_sensor_skips_actions_check(
    isy_data, controller, caplog
):
    """``Platform.BINARY_SENSOR`` programs are status-only (no
    actions program is expected); the warning shouldn't fire."""
    status = make_program(
        make_program_record(
            "0020",
            "status",
            path="HA.binary_sensor/Bar/status",
            status=False,
        ),
        controller,
    )

    _categorize_programs(isy_data, {"0020": status})

    assert isy_data.programs[Platform.BINARY_SENSOR] == [("Bar", status, None)]
    assert not any("missing actions program" in m for m in caplog.messages)


def test_categorize_programs_ignores_paths_outside_HA_namespace(isy_data, controller):
    """Programs without an ``HA.<platform>/`` ancestor stay outside
    HA entity construction — the user uses them server-side only."""
    other = make_program(
        make_program_record("0030", "My Routine", path="My Programs/My Routine"),
        controller,
    )

    _categorize_programs(isy_data, {"0030": other})

    for platform_programs in isy_data.programs.values():
        assert platform_programs == []


def test_categorize_programs_skips_ignore_string_in_program_name(isy_data, controller):
    """#62: a legacy HA.<platform>/ program whose own name carries the
    ignore string is not surfaced."""
    status = make_program(
        make_program_record("0010", "status", path="HA.switch/Foo {IGNORE ME}/status"),
        controller,
    )
    actions = make_program(
        make_program_record(
            "0011", "actions", path="HA.switch/Foo {IGNORE ME}/actions"
        ),
        controller,
    )

    _categorize_programs(
        isy_data, {"0010": status, "0011": actions}, ignore_identifier="{IGNORE ME}"
    )

    assert isy_data.programs[Platform.SWITCH] == []


def test_categorize_programs_skips_ignore_string_in_containing_folder(
    isy_data, controller
):
    """#62: the ignore string in a *containing folder* (path is the
    slash-joined folder/name chain) suppresses everything under it,
    even when the program's own leaf name is clean."""
    status = make_program(
        make_program_record(
            "0010", "status", path="HA.switch/{IGNORE ME} Group/Bar/status"
        ),
        controller,
    )
    actions = make_program(
        make_program_record(
            "0011", "actions", path="HA.switch/{IGNORE ME} Group/Bar/actions"
        ),
        controller,
    )

    _categorize_programs(
        isy_data, {"0010": status, "0011": actions}, ignore_identifier="{IGNORE ME}"
    )

    assert isy_data.programs[Platform.SWITCH] == []


def test_categorize_program_devices_skips_ignore_string(isy_data, controller):
    """#62: program-as-device fan-out honors the ignore string for both
    the program's own name and any containing folder; clean programs
    still fan out."""
    by_name = make_program(
        make_program_record(
            "0030", "My {IGNORE ME} Routine", path="Lighting/My {IGNORE ME} Routine"
        ),
        controller,
    )
    by_folder = make_program(
        make_program_record("0031", "Clean", path="{IGNORE ME} Folder/Clean"),
        controller,
    )
    keeper = make_program(
        make_program_record("0032", "Keeper", path="Lighting/Keeper"),
        controller,
    )

    _categorize_program_devices(
        isy_data,
        {"0030": by_name, "0031": by_folder, "0032": keeper},
        program_prefix="HA.",
        ignore_identifier="{IGNORE ME}",
    )

    assert keeper in isy_data.program_devices
    assert by_name not in isy_data.program_devices
    assert by_folder not in isy_data.program_devices


# --- suggested_area: derived from IoX folder ancestry ---------------------


def test_suggested_area_uses_immediate_parent_folder(isy_data, options):
    """A node whose ``parent_address`` is a folder gets that folder's
    name as ``suggested_area`` — mirrors HA core ``isy994``'s
    ``node.folder``-as-area pattern."""
    from pyisyox.testing import make_folder_record

    folder = make_folder_record("F1", "Living Room")
    node_rec = make_node_record("1A 2B 3C 1", "Lamp", parent_address="F1")
    controller = make_controller(
        make_load_result(
            nodes={node_rec.address: node_rec}, folders={folder.address: folder}
        )
    )
    nodes = {node_rec.address: make_node(node_rec, controller)}

    _categorize_nodes(
        isy_data, nodes, options, controller=controller, host="https://eisy.local"
    )

    assert isy_data.devices["1A 2B 3C 1"].get("suggested_area") == "Living Room"


def test_suggested_area_climbs_through_plugin_controller_to_find_folder(
    isy_data, options
):
    """A NODE_SERVER plugin child whose ``parent_address`` points to
    its plugin controller (not directly to a folder) climbs through
    the controller node to find the enclosing folder. Exercises the
    ``controller.nodes.get(address)`` branch in
    ``_suggested_area_for_node``.

    Insteon sub-buttons share an HA device with their primary so
    ``_generate_device_info`` is never called for them — only
    NODE_SERVER children get their own ``DeviceInfo`` and therefore
    their own ``suggested_area`` derivation, which is when the
    node-walk path actually fires.
    """
    from pyisyox.testing import (
        PLUGIN_COVER_FAMILY_ID,
        PLUGIN_COVER_INSTANCE_ID,
        PLUGIN_COVER_NODEDEF_ID,
        make_folder_record,
    )

    folder = make_folder_record("F1", "Garage")
    plugin_controller = make_node_record(
        "n100_controller",
        "Plugin Hub",
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        type_="",
        parent_address="F1",
    )
    plugin_child = make_node_record(
        "n100_blind1",
        "Garage Blind",
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        pnode="n100_controller",
        parent_address="n100_controller",
        type_="",
    )
    controller = make_controller(
        make_load_result(
            nodes={
                plugin_controller.address: plugin_controller,
                plugin_child.address: plugin_child,
            },
            folders={folder.address: folder},
        )
    )
    nodes = {
        rec.address: make_node(rec, controller)
        for rec in (plugin_controller, plugin_child)
    }

    _categorize_nodes(
        isy_data, nodes, options, controller=controller, host="https://eisy.local"
    )

    # The child's parent_address is another node — the walk has to
    # climb through it before finding the folder.
    assert isy_data.devices["n100_blind1"].get("suggested_area") == "Garage"
    # And the controller still finds the folder in one hop.
    assert isy_data.devices["n100_controller"].get("suggested_area") == "Garage"


def test_suggested_area_innermost_folder_wins_in_nested_chain(isy_data, options):
    """When a node sits in ``Lighting/Living Room``, the immediate
    (innermost) parent folder wins — not the outer ancestor."""
    from pyisyox.testing import make_folder_record

    outer = make_folder_record("F1", "Lighting")
    inner = make_folder_record("F2", "Living Room", parent_address="F1")
    node_rec = make_node_record("1A 2B 3C 1", "Lamp", parent_address="F2")
    controller = make_controller(
        make_load_result(
            nodes={node_rec.address: node_rec},
            folders={outer.address: outer, inner.address: inner},
        )
    )
    nodes = {node_rec.address: make_node(node_rec, controller)}

    _categorize_nodes(
        isy_data, nodes, options, controller=controller, host="https://eisy.local"
    )

    assert isy_data.devices["1A 2B 3C 1"].get("suggested_area") == "Living Room"


def test_suggested_area_root_node_has_none(isy_data, options, controller):
    """A node with no ``parent_address`` (root of the IoX tree) leaves
    ``suggested_area`` unset so HA picks the user's manual area."""
    node_rec = make_node_record("1A 2B 3C 1", "Lamp")
    nodes = {node_rec.address: make_node(node_rec, controller)}

    _categorize_nodes(
        isy_data, nodes, options, controller=controller, host="https://eisy.local"
    )

    assert isy_data.devices["1A 2B 3C 1"].get("suggested_area") is None


# --- convert_isy_value_to_hass coverage push ---


def test_convert_isy_value_handles_double_temp_uom() -> None:
    """``UOM_DOUBLE_TEMP`` divides the raw value by two and rounds
    to one decimal."""
    from custom_components.udi_iox.const import UOM_DOUBLE_TEMP
    from custom_components.udi_iox.helpers import convert_isy_value_to_hass

    assert convert_isy_value_to_hass(140, UOM_DOUBLE_TEMP, "0") == 70.0
    assert convert_isy_value_to_hass(141, UOM_DOUBLE_TEMP, "0") == 70.5


def test_convert_isy_value_returns_none_for_empty_or_unparseable() -> None:
    """Empty / non-numeric / None inputs return None."""
    from custom_components.udi_iox.helpers import convert_isy_value_to_hass

    assert convert_isy_value_to_hass(None, "1", "0") is None
    assert convert_isy_value_to_hass("", "1", "0") is None
    assert convert_isy_value_to_hass("not-a-number", "1", "0") is None


def test_convert_isy_value_uses_fallback_precision() -> None:
    """``fallback_precision`` rounds when the property has no
    declared precision."""
    from custom_components.udi_iox.helpers import convert_isy_value_to_hass

    assert convert_isy_value_to_hass(1.23456, "1", "0", fallback_precision=2) == 1.23


def test_convert_isy_value_returns_raw_when_no_precision_or_fallback() -> None:
    """A precision of 0 with no fallback returns the raw float."""
    from custom_components.udi_iox.helpers import convert_isy_value_to_hass

    assert convert_isy_value_to_hass(42, "1", "0") == 42.0


def test_convert_isy_value_scales_by_explicit_precision() -> None:
    """Non-zero precision divides by 10**precision."""
    from custom_components.udi_iox.helpers import convert_isy_value_to_hass

    assert convert_isy_value_to_hass(760, "17", "1") == 76.0


def test_primary_platform_for_native_lock_branch() -> None:
    """An ``is_lock`` node short-circuits to LOCK regardless of the
    classifier."""
    from unittest.mock import MagicMock

    from homeassistant.const import Platform

    from custom_components.udi_iox.helpers import _primary_platform_for_native

    node = MagicMock()
    node.is_thermostat = False
    node.is_lock = True
    node.is_fan = False
    assert _primary_platform_for_native(node, None) == Platform.LOCK


def test_primary_platform_for_native_falls_back_to_switch_when_classifier_missing() -> (
    None
):
    """When all type-introspection flags are False AND classifier
    result is None, the historical SWITCH fallback fires."""
    from unittest.mock import MagicMock

    from homeassistant.const import Platform

    from custom_components.udi_iox.helpers import _primary_platform_for_native

    node = MagicMock()
    node.is_thermostat = False
    node.is_lock = False
    node.is_fan = False
    assert _primary_platform_for_native(node, None) == Platform.SWITCH


def test_categorize_skips_groups_with_ignore_identifier_in_name() -> None:
    """A group whose name contains ``CONF_IGNORE_STRING`` is silently
    skipped."""
    from types import MappingProxyType

    from pyisyox.testing import (
        make_controller,
        make_group_record,
        make_load_result,
    )

    from custom_components.udi_iox.helpers import _categorize_nodes
    from custom_components.udi_iox.models import IsyData

    ignored = make_group_record("99001", "{IGNORE ME} Hidden Scene")
    visible = make_group_record("99002", "Visible Scene")
    controller = make_controller(
        make_load_result(groups={ignored.address: ignored, visible.address: visible})
    )
    isy_data = IsyData()
    isy_data.root = controller
    _categorize_nodes(
        isy_data,
        controller.nodes,
        MappingProxyType({}),
        controller=controller,
        host="http://localhost",
    )
    addresses = [g.address for g in isy_data.groups]
    assert "99002" in addresses
    assert "99001" not in addresses


def test_categorize_routes_sensor_marked_groups_to_group_sensors() -> None:
    """A scene whose name carries the ``sensor_string`` marker is forced
    read-only: it lands on ``group_sensors`` (→ binary_sensor) instead
    of ``groups`` (→ switch). Unmarked scenes are unaffected
    (hacs-udi-iox#84)."""
    from types import MappingProxyType

    from pyisyox.testing import (
        make_controller,
        make_group_record,
        make_load_result,
    )

    from custom_components.udi_iox.helpers import _categorize_nodes
    from custom_components.udi_iox.models import IsyData

    marked = make_group_record("99003", "Garbage Disposal {SENSOR}")
    plain = make_group_record("99004", "Living Room Scene")
    controller = make_controller(
        make_load_result(groups={marked.address: marked, plain.address: plain})
    )
    isy_data = IsyData()
    isy_data.root = controller
    _categorize_nodes(
        isy_data,
        controller.nodes,
        MappingProxyType({}),
        controller=controller,
        host="http://localhost",
    )

    switch_addrs = [g.address for g in isy_data.groups]
    sensor_addrs = [g.address for g in isy_data.group_sensors]
    assert "99004" in switch_addrs and "99004" not in sensor_addrs
    assert "99003" in sensor_addrs and "99003" not in switch_addrs

    # The forced-sensor scene must claim a binary_sensor unique id (so
    # stale-entity cleanup matches the platform it's actually created
    # on), not a switch one.
    assert (
        Platform.BINARY_SENSOR,
        isy_data.uid_base(marked),
    ) in isy_data.unique_ids
    assert (Platform.SWITCH, isy_data.uid_base(marked)) not in isy_data.unique_ids


def test_categorize_routes_groups_by_capability_bits() -> None:
    """Scene → HA platform from the pyisyox link-target bits
    (hacs-udi-iox#86):

    * ``has_state_target`` False (fire-only) → ``group_buttons`` (button)
    * else ``has_dimmable_members`` → ``group_lights`` (light)
    * else → ``groups`` (switch, the default)
    * an explicit ``sensor_string`` marker still wins over all of the
      above → ``group_sensors`` (binary_sensor)
    """
    from types import MappingProxyType

    from pyisyox.testing import (
        make_controller,
        make_group_record,
        make_load_result,
        make_node_record,
    )

    from custom_components.udi_iox.helpers import _categorize_nodes
    from custom_components.udi_iox.models import IsyData

    # Fire-only: pyisyox Group.has_state_target is False when targets
    # *are* resolved but no member resolved to an on/off intent — that's
    # what these two field settings produce (coupling to pyisyox 6.0.0b9
    # internals, pinned).
    fire_only = make_group_record("99101", "Doorbell Chime")
    fire_only.targets_resolved = True
    fire_only.member_intents = {}

    # State-maintained (targets unresolved → assume stateful) with a
    # dimmable member → light.
    dimmer_member = make_node_record("99 30 01 1", "Lamp")  # default = DimmerLampSwitch
    dimmable = make_group_record(
        "99201", "Dining Scene", member_addresses=("99 30 01 1",)
    )

    # State-maintained, no dimmable member → switch (default, unchanged).
    plain_switch = make_group_record("99301", "Outlet Scene")

    # Fire-only AND sensor-marked: the explicit marker must win over the
    # capability-derived button routing.
    marked_fire = make_group_record("99401", "Test Scene {SENSOR}")
    marked_fire.targets_resolved = True
    marked_fire.member_intents = {}

    controller = make_controller(
        make_load_result(
            nodes={dimmer_member.address: dimmer_member},
            groups={
                fire_only.address: fire_only,
                dimmable.address: dimmable,
                plain_switch.address: plain_switch,
                marked_fire.address: marked_fire,
            },
        )
    )
    isy_data = IsyData()
    isy_data.root = controller
    _categorize_nodes(
        isy_data,
        controller.nodes,
        MappingProxyType({}),
        controller=controller,
        host="http://localhost",
    )

    buttons = [g.address for g in isy_data.group_buttons]
    lights = [g.address for g in isy_data.group_lights]
    switches = [g.address for g in isy_data.groups]
    sensors = [g.address for g in isy_data.group_sensors]

    assert buttons == ["99101"]
    assert lights == ["99201"]
    assert switches == ["99301"]
    assert sensors == ["99401"]

    # Each scene claims exactly the unique id for the platform it's
    # actually created on (clean stale-entity migration off switch).
    uids = isy_data.unique_ids
    assert (Platform.BUTTON, isy_data.uid_base(fire_only)) in uids
    assert (Platform.LIGHT, isy_data.uid_base(dimmable)) in uids
    assert (Platform.SWITCH, isy_data.uid_base(plain_switch)) in uids
    assert (Platform.BINARY_SENSOR, isy_data.uid_base(marked_fire)) in uids
    for stale in (fire_only, dimmable, marked_fire):
        assert (Platform.SWITCH, isy_data.uid_base(stale)) not in uids
