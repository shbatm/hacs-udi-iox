"""Tests for helpers._categorize_nodes — the type-based + classifier
two-tier platform router.

Pins behavior that's invisible from a single function read:
- which native node attributes drive which HA platform
- how the sub-button suppression rule kicks in vs. doesn't
- aux-property fan-out (binary UOMs vs. sensor)
- plugin nodedefs falling through to pyisyox.classify

The tests build real :class:`pyisyox.Node` instances via
:mod:`tests.builders` — introspection (``is_thermostat`` / ``is_lock`` /
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
from pyisyox import Node, NodePropertyValue

from custom_components.udi_iox.helpers import _categorize_nodes, _categorize_programs
from custom_components.udi_iox.models import IsyData
from tests.builders import (
    make_classified_node_record,
    make_controller,
    make_load_result,
    make_node,
    make_node_record,
    make_program,
    make_program_record,
)


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


def test_subbutton_with_parent_suppresses_switch_keeps_event(
    isy_data, options, controller
):
    """KeypadLinc-style sub-button: primary_address set, classifies
    as SWITCH, Insteon — must end up EVENT-only."""
    node = _node(controller, "AA BB CC 2", target="subbutton", pnode="AA BB CC 1")
    _categorize(isy_data, node, options, controller=controller)

    assert isy_data.nodes[Platform.SWITCH] == []
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_subbutton_dimmer_paddle_keeps_light_classification(
    isy_data, options, controller
):
    """A dimmable sub-node (BRT/DIM accept commands) is a real load
    surface, not a button — keep primary=LIGHT + parallel=EVENT."""
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
    # EVENT branch is also gated on Insteon, so a Z-Wave sub-node
    # gets switch-only.
    assert isy_data.nodes[Platform.EVENT] == []


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
    """Aux properties with UOM 2 / 78 / 79 fan out to BINARY_SENSOR,
    not SENSOR."""
    record = make_classified_node_record(
        "AA BB CC 1",
        "Mixed-UOM Device",
        target="switch",
        properties={
            "ST": NodePropertyValue(id="ST", value="100", uom="51"),
            # GVx aux property with UOM 2 → binary
            "GV1": NodePropertyValue(id="GV1", value="0", uom="2"),
            # GVy aux property with a non-binary UOM → sensor
            "GV2": NodePropertyValue(id="GV2", value="50", uom="51"),
        },
    )
    node = make_node(record, controller)
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


# --- node-server device hierarchy ------------------------------------


def test_node_server_children_get_own_device_info(isy_data, options, controller):
    """A plugin controller + 2 plugin children should each receive
    their own :class:`DeviceInfo` rather than the children folding
    under the controller — matches the eisy UI per-sensor cards and
    avoids "Current"/"Leak Detected" name collisions when siblings
    share aux property labels.
    """
    from custom_components.udi_iox.const import DOMAIN
    from tests.builders import (
        PLUGIN_COVER_FAMILY_ID,
        PLUGIN_COVER_INSTANCE_ID,
        PLUGIN_COVER_NODEDEF_ID,
    )

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
    from tests.builders import (
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
    """A PG3 dimmer's parameterised setters land on the HA platform
    their *editor* implies: a pure-enum editor (``names``, no numeric
    bounds) → SELECT; the generic ``INTEGER`` editor → NUMBER. The node
    itself routes onto LIGHT via the classifier's controllable.
    """
    from tests.builders import (
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
    assert (node, "SETMODE") in isy_data.aux_properties[Platform.SELECT]
    assert (node, "THRESHOLD") in isy_data.aux_properties[Platform.NUMBER]
    # SETMODE's enum editor must not be mistaken for a slider, and the
    # INTEGER setter must not become a 1000-option dropdown.
    assert (node, "SETMODE") not in isy_data.aux_properties[Platform.NUMBER]
    assert (node, "THRESHOLD") not in isy_data.aux_properties[Platform.SELECT]


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
