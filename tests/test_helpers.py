"""Tests for helpers._categorize_nodes — the type-based + classifier
two-tier platform router.

Pins behavior that's invisible from a single function read:
- which native node attributes drive which HA platform
- how the sub-button suppression rule kicks in vs. doesn't
- aux-property fan-out (binary UOMs vs. sensor)
- plugin nodedefs falling through to pyisyox.classify

The tests build FakeNode instances with the exact introspection
shape that pyisyox would produce for the case under test.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.const import Platform

from custom_components.udi_iox.helpers import _categorize_nodes
from custom_components.udi_iox.models import IsyData


@pytest.fixture
def isy_data():
    return IsyData()


@pytest.fixture
def options():
    return MappingProxyType({})


def _categorize(
    isy_data: IsyData,
    node: Any,
    options: MappingProxyType[str, Any],
    *,
    controller: Any,
    host: str = "https://eisy.local:443",
) -> None:
    nodes = {node.address: node}
    _categorize_nodes(isy_data, nodes, options, controller=controller, host=host)


# --- primary platform routing -----------------------------------------


def test_thermostat_classifies_as_climate(
    isy_data, options, fake_node_factory, fake_controller
):
    node = fake_node_factory(
        address="A 1", is_thermostat=True, parent_address=None
    )
    _categorize(isy_data, node, options, controller=fake_controller)
    assert isy_data.nodes[Platform.CLIMATE] == [node]


def test_lock_classifies_as_lock(
    isy_data, options, fake_node_factory, fake_controller
):
    node = fake_node_factory(address="A 1", is_lock=True, parent_address=None)
    _categorize(isy_data, node, options, controller=fake_controller)
    assert isy_data.nodes[Platform.LOCK] == [node]


def test_dimmable_classifies_as_light(
    isy_data, options, fake_node_factory, fake_controller
):
    node = fake_node_factory(
        address="A 1", is_dimmable=True, parent_address=None
    )
    _categorize(isy_data, node, options, controller=fake_controller)
    assert isy_data.nodes[Platform.LIGHT] == [node]


def test_default_native_classifies_as_switch(
    isy_data, options, fake_node_factory, fake_controller
):
    node = fake_node_factory(address="A 1", parent_address=None)
    _categorize(isy_data, node, options, controller=fake_controller)
    assert isy_data.nodes[Platform.SWITCH] == [node]


# --- sub-button suppression -------------------------------------------


def test_subbutton_with_parent_suppresses_switch_keeps_event(
    isy_data, options, fake_node_factory, fake_controller
):
    """KeypadLinc-style sub-button: parent_address set, classifies
    as SWITCH, Insteon — must end up EVENT-only."""
    node = fake_node_factory(
        address="AA BB CC 2",
        parent_address="AA BB CC 1",
        protocol="insteon",
    )
    _categorize(isy_data, node, options, controller=fake_controller)

    assert isy_data.nodes[Platform.SWITCH] == []
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_subbutton_dimmer_paddle_keeps_light_classification(
    isy_data, options, fake_node_factory, fake_controller
):
    """A dimmable sub-node (BRT/DIM accept commands) is a real load
    surface, not a button — keep primary=LIGHT + parallel=EVENT."""
    node = fake_node_factory(
        address="AA BB CC 2",
        parent_address="AA BB CC 1",
        protocol="insteon",
        is_dimmable=True,
    )
    _categorize(isy_data, node, options, controller=fake_controller)

    assert isy_data.nodes[Platform.LIGHT] == [node]
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_root_load_keeps_switch_classification(
    isy_data, options, fake_node_factory, fake_controller
):
    """Root SWITCH-shape Insteon node → SWITCH + EVENT, NOT
    suppressed."""
    node = fake_node_factory(
        address="AA BB CC 1", parent_address=None, protocol="insteon"
    )
    _categorize(isy_data, node, options, controller=fake_controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    assert isy_data.nodes[Platform.EVENT] == [node]


def test_subbutton_non_insteon_not_suppressed(
    isy_data, options, fake_node_factory, fake_controller
):
    """The sub-button rule is Insteon-specific; Z-Wave sub-nodes (rare
    but real on multi-endpoint devices) shouldn't be silently
    dropped."""
    node = fake_node_factory(
        address="ZW 2",
        parent_address="ZW 1",
        protocol="zwave",
    )
    _categorize(isy_data, node, options, controller=fake_controller)

    assert isy_data.nodes[Platform.SWITCH] == [node]
    # EVENT branch is also gated on Insteon, so a Z-Wave sub-node
    # gets switch-only.
    assert isy_data.nodes[Platform.EVENT] == []


# --- aux property fan-out ---------------------------------------------


def test_root_dimmer_fans_aux_props_to_number_select(
    isy_data, options, fake_node_factory, fake_controller
):
    """Root dimmable nodes spawn NUMBER (on_level) + SELECT
    (ramp_rate) entities for the matching aux properties."""
    from tests._fakes import FakeNodePropertyValue as _PV

    node = fake_node_factory(
        address="AA BB CC 1",
        parent_address=None,
        is_dimmable=True,
        properties={
            "ST": _PV(id="ST", value="100", uom="51"),
            "OL": _PV(id="OL", value="200", uom="100"),
            "RR": _PV(id="RR", value="20", uom="25"),
        },
    )
    _categorize(isy_data, node, options, controller=fake_controller)

    on_level_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.NUMBER]]
    ramp_rate_aux = [(n, c) for n, c in isy_data.aux_properties[Platform.SELECT]]
    assert (node, "OL") in on_level_aux
    assert (node, "RR") in ramp_rate_aux


def test_aux_property_with_binary_uom_routes_to_binary_sensor(
    isy_data, options, fake_node_factory, fake_controller
):
    """Aux properties with UOM 2 / 78 / 79 fan out to BINARY_SENSOR,
    not SENSOR."""
    from tests._fakes import FakeNodePropertyValue as _PV

    node = fake_node_factory(
        address="AA BB CC 1",
        parent_address=None,
        properties={
            "ST": _PV(id="ST", value="100", uom="51"),
            # GVx aux property with UOM 2 → binary
            "GV1": _PV(id="GV1", value="0", uom="2"),
            # GVy aux property with a non-binary UOM → sensor
            "GV2": _PV(id="GV2", value="50", uom="51"),
        },
    )
    _categorize(isy_data, node, options, controller=fake_controller)

    assert (node, "GV1") in [
        (n, c) for n, c in isy_data.aux_properties[Platform.BINARY_SENSOR]
    ]
    assert (node, "GV2") in [
        (n, c) for n, c in isy_data.aux_properties[Platform.SENSOR]
    ]


# --- ignore / sensor identifier ---------------------------------------


def test_ignore_string_skips_node(isy_data, fake_node_factory, fake_controller):
    """A node whose name contains the configured ignore string is
    excluded from every platform."""
    node = fake_node_factory(
        address="A 1", parent_address=None, name="Hallway {IGNORE ME}"
    )
    opts = MappingProxyType({"ignore_string": "{IGNORE ME}"})
    _categorize(isy_data, node, opts, controller=fake_controller)

    for platform in (
        Platform.LIGHT,
        Platform.SWITCH,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.EVENT,
    ):
        assert isy_data.nodes[platform] == []


def test_sensor_identifier_forces_sensor_classification(
    isy_data, fake_node_factory, fake_controller
):
    """A node whose name contains the sensor identifier is forced to
    SENSOR, bypassing the type-based introspection. Useful for
    custom dimmable devices the user wants reported as a sensor."""
    from tests._fakes import FakeNodePropertyValue as _PV

    node = fake_node_factory(
        address="A 1",
        parent_address=None,
        is_dimmable=True,  # Would normally classify as LIGHT
        name="Outdoor Lux sensor",
        properties={"ST": _PV(id="ST", value="500", uom="36")},
    )
    opts = MappingProxyType({"sensor_string": "sensor"})
    _categorize(isy_data, node, opts, controller=fake_controller)

    assert isy_data.nodes[Platform.SENSOR] == [node]
    assert isy_data.nodes[Platform.LIGHT] == []


# --- guard rail -------------------------------------------------------


def test_categorize_no_op_when_controller_is_none(
    isy_data, options, fake_node_factory
):
    """Defensive default — older test fixtures invoke
    _categorize_nodes without a controller; the function returns
    cleanly so the test isn't forced to mock the full surface."""
    node = fake_node_factory(address="A 1")
    _categorize_nodes(isy_data, {"A 1": node}, options, controller=None)
    assert isy_data.nodes[Platform.SWITCH] == []
