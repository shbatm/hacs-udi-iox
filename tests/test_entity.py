"""Direct unit tests for udi_iox/entity.py shared scaffolding."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from pyisyox.client import NodePropertyValue
from pyisyox.constants import ISY_VALUE_UNKNOWN
from pyisyox.testing import (
    make_controller,
    make_load_result,
    make_node,
    make_node_record,
)

from custom_components.udi_iox.entity import (
    _common_token_run,
    _pnode_group_naming,
    _primary_status_label,
    _strip_parent_prefix,
    aux_entity_category,
    node_status_int,
)
from tests.conftest import isy_data_for


def test_aux_entity_category_st_is_primary_control() -> None:
    """A coalesced control that writes the node's status (``ST`` — a
    node-server ``virtualtemp`` setpoint, an i3 flags ``GV0`` "Mode")
    is the device's main control: no category. Every other aux control
    is configuration."""
    assert aux_entity_category("ST") is None
    assert aux_entity_category("GV1") is EntityCategory.CONFIG
    assert aux_entity_category("OL") is EntityCategory.CONFIG
    assert aux_entity_category("enabled") is EntityCategory.CONFIG


def test_node_status_int_returns_none_on_unknown_value() -> None:
    """``node.status.value == ISY_VALUE_UNKNOWN`` short-circuits to None."""
    fake = MagicMock()
    fake.status = NodePropertyValue(
        id="ST", value=ISY_VALUE_UNKNOWN, formatted="?", uom="0", name="Status"
    )
    assert node_status_int(fake) is None


def test_node_status_int_returns_none_on_unparseable_value() -> None:
    """A non-numeric ``value`` returns None."""
    fake = MagicMock()
    fake.status = NodePropertyValue(
        id="ST", value="banana", formatted="?", uom="0", name="Status"
    )
    assert node_status_int(fake) is None


def test_strip_parent_prefix_removes_label_prefix() -> None:
    """Sub-node labels prefixed with the parent name get the prefix
    stripped so HA's auto-name composition (parent + this) doesn't
    duplicate it."""
    assert _strip_parent_prefix("Hallway Light Button A", "Hallway Light") == "Button A"
    # No common prefix → return as-is.
    assert _strip_parent_prefix("Foo Bar", "Other") == "Foo Bar"
    # No parent → return as-is.
    assert _strip_parent_prefix("Foo Bar", None) == "Foo Bar"
    # Shared leading run, but the device name carries a distinguishing
    # suffix the child lacks (not a verbatim prefix) — token compare.
    assert (
        _strip_parent_prefix(
            "Kitchen Refrigerator Leak HB", "Kitchen Refrigerator Leak.Dry"
        )
        == "HB"
    )
    assert (
        _strip_parent_prefix("Main Bedroom Ceiling Fan", "Main Bedroom Fan Light")
        == "Ceiling Fan"
    )
    # Remainder punctuation/casing is preserved verbatim (not re-joined).
    assert (
        _strip_parent_prefix("Bedroom Hall KP.A-Hallway", "Bedroom Hall")
        == "KP.A-Hallway"
    )
    # Child wholly contained in the device name → keep full label.
    assert _strip_parent_prefix("Hallway Light", "Hallway Light") == "Hallway Light"
    # Hyphen-separated IoX naming (BinaryAlarm_ADV Motion Sensor): the
    # leading separator must NOT survive — slice starts at the first
    # divergent token, so "-Dusk.Dawn" → "Dusk.Dawn".
    assert (
        _strip_parent_prefix("Motion Sensor-Dusk.Dawn", "Motion Sensor-Sensor")
        == "Dusk.Dawn"
    )
    assert (
        _strip_parent_prefix("Motion Sensor-Low Bat", "Motion Sensor-Sensor")
        == "Low Bat"
    )
    # Same after the user renames the primary "Motion Sensor-Sensor" →
    # "Motion Sensor": still "Dusk.Dawn", never "-Dusk.Dawn".
    assert (
        _strip_parent_prefix("Motion Sensor-Dusk.Dawn", "Motion Sensor") == "Dusk.Dawn"
    )
    assert _strip_parent_prefix("Motion Sensor-Low Bat", "Motion Sensor") == "Low Bat"


def test_common_token_run_counts_shared_leading_tokens() -> None:
    """Leading tokens common (case-insensitive, ``\\s.-:_``-delimited)
    to every name; 0 when the list is empty or any name is tokenless."""
    assert (
        _common_token_run(
            [
                "Primary Bath Toilet Leak.Dry",
                "Primary Bath Toilet Leak.Wet",
                "Primary Bath Toilet Leak.HB",
            ]
        )
        == 4
    )
    # Case-insensitive; one outlier truncates the run.
    assert _common_token_run(["main bedroom fan", "Main Bedroom Light"]) == 2
    assert _common_token_run(["Foo Bar", "Other Bar"]) == 0
    assert _common_token_run([]) == 0
    assert _common_token_run(["Foo", ""]) == 0
    assert _common_token_run(["Solo Name"]) == 2


def _node(name: str, address: str, *, pnode: str | None = None, proto: str = "insteon"):
    """Minimal duck-typed stand-in for ``_pnode_group_naming``."""
    from types import SimpleNamespace

    return SimpleNamespace(
        name=name, address=address, primary_address=pnode, protocol=proto
    )


def test_pnode_group_naming_collapses_facet_delimited_groups() -> None:
    """Shared prefix + a *non-space* separator (``.``/``-``) on every
    member incl. the primary = one multi-facet device → device = prefix,
    primary entity = residual. Leak (``.``), motion sensor (``-``,
    mixed-length discriminators), dual outlet (``.``)."""
    dry = _node("Primary Bath Toilet Leak.Dry", "54 8F C0 1")
    wet = _node("Primary Bath Toilet Leak.Wet", "54 8F C0 2", pnode="54 8F C0 1")
    hb = _node("Primary Bath Toilet Leak.HB", "54 8F C0 4", pnode="54 8F C0 1")
    assert _pnode_group_naming({n.address: n for n in (dry, wet, hb)}, dry) == (
        "Primary Bath Toilet Leak",
        "Dry",
    )
    sensor = _node("Motion Sensor-Sensor", "2D AF 71 1")
    sdd = _node("Motion Sensor-Dusk.Dawn", "2D AF 71 2", pnode="2D AF 71 1")
    slb = _node("Motion Sensor-Low Bat", "2D AF 71 3", pnode="2D AF 71 1")
    assert _pnode_group_naming({n.address: n for n in (sensor, sdd, slb)}, sensor) == (
        "Motion Sensor",
        "Sensor",
    )
    top = _node("Test Outlet.1 On-Off Top", "41 8A 6E 1")
    bot = _node("Test Outlet.2 On-Off Bot", "41 8A 6E 2", pnode="41 8A 6E 1")
    assert _pnode_group_naming({n.address: n for n in (top, bot)}, top) == (
        "Test Outlet",
        "1 On-Off Top",
    )


def test_pnode_group_naming_primary_is_prefix_stays_unnamed() -> None:
    """Primary name *is* the shared run (no residual) → primary unnamed,
    device keeps the name. KeypadLinc "Hallway Keypad" + buttons; and
    the motion-sensor end state once the user renames "Motion
    Sensor-Sensor" → "Motion Sensor" (device "Motion Sensor", primary
    unnamed, subs strip to "Dusk.Dawn"/"Low Bat")."""
    kp = _node("Hallway Keypad", "AA 1 1 1")
    a = _node("Hallway Keypad A", "AA 1 1 3", pnode="AA 1 1 1")
    b = _node("Hallway Keypad B", "AA 1 1 4", pnode="AA 1 1 1")
    assert _pnode_group_naming({n.address: n for n in (kp, a, b)}, kp) == (
        "Hallway Keypad",
        None,
    )
    renamed = _node("Motion Sensor", "2D AF 71 1")
    dd = _node("Motion Sensor-Dusk.Dawn", "2D AF 71 2", pnode="2D AF 71 1")
    lb = _node("Motion Sensor-Low Bat", "2D AF 71 3", pnode="2D AF 71 1")
    assert _pnode_group_naming({n.address: n for n in (renamed, dd, lb)}, renamed) == (
        "Motion Sensor",
        None,
    )


def test_pnode_group_naming_skips_space_delimited_compound() -> None:
    """A plain space after the shared prefix is an ordinary compound
    name, not a facet marker → no collapse, device keeps its name, the
    sub is handled by _strip_parent_prefix as before (no churn). A
    "Hallway Light" dimmer + "Hallway Button B"; a FanLinc "FanLinc
    Lamp" + "FanLinc Motor"."""
    light = _node("Hallway Light", "AA AA AA 1")
    btn = _node("Hallway Button B", "AA AA AA 2", pnode="AA AA AA 1")
    assert _pnode_group_naming({n.address: n for n in (light, btn)}, light) == (
        "Hallway Light",
        None,
    )
    lamp = _node("FanLinc Lamp", "EE EE EE 1")
    motor = _node("FanLinc Motor", "EE EE EE 2", pnode="EE EE EE 1")
    assert _pnode_group_naming({n.address: n for n in (lamp, motor)}, lamp) == (
        "FanLinc Lamp",
        None,
    )


def test_primary_status_label_node_server_st_name_else_none() -> None:
    """pnode residual wins; else a node-server nodedef ST name
    ("Current" on virtualtemp); native ST stays unnamed (gated)."""
    from types import SimpleNamespace as N

    def nd(st_name: str | None):
        st = N(name=st_name) if st_name is not None else None
        return N(properties={"ST": st} if st is not None else {})

    ns = N(protocol="node_server")
    insteon = N(protocol="insteon")
    # pnode residual always wins, even with an ST name present.
    assert _primary_status_label(ns, nd("Current"), "Dry") == "Dry"
    # node-server + named ST → that name.
    assert _primary_status_label(ns, nd("Current"), None) == "Current"
    # node-server but ST unnamed / missing / no nodedef → unnamed.
    assert _primary_status_label(ns, nd(""), None) is None
    assert _primary_status_label(ns, nd(None), None) is None
    assert _primary_status_label(ns, None, None) is None
    # The generic "Status" token is stripped: bare "Status" (PG3 cover,
    # "38 dimmer") → nothing → unnamed (any case); "Switch Status"
    # ("39 switch" nodedef quirk) → "Switch".
    assert _primary_status_label(ns, nd("Status"), None) is None
    assert _primary_status_label(ns, nd("status"), None) is None
    assert _primary_status_label(ns, nd("Switch Status"), None) == "Switch"
    # native node with a named ST is still unnamed (gate is node-server).
    assert _primary_status_label(insteon, nd("Status"), None) is None


def test_pnode_group_naming_lone_node_unchanged() -> None:
    """No folded sub-nodes → name verbatim, primary unnamed."""
    solo = _node("Kitchen Light", "BB 2 2 1")
    assert _pnode_group_naming({solo.address: solo}, solo) == ("Kitchen Light", None)


def test_pnode_group_naming_ignores_node_server_children() -> None:
    """A node-server child carries primary_address but owns its device —
    it must not shorten the controller node's name."""
    ctrl = _node("Flume Water", "n012 ctrl")
    child = _node(
        "Flume Sensor 7061", "n012 7061", pnode="n012 ctrl", proto="node_server"
    )
    nodes = {n.address: n for n in (ctrl, child)}
    assert _pnode_group_naming(nodes, ctrl) == ("Flume Water", None)


def test_isy_entity_unavailable_when_ws_disconnected() -> None:
    """``ISYEntity.available`` returns False when the WS is down."""
    from custom_components.udi_iox.entity import ISYEntity

    controller = make_controller(make_load_result())
    make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    isy_data.controller_events.ws_connected = False  # type: ignore[attr-defined]

    entity = ISYEntity.__new__(ISYEntity)
    entity._isy_data = isy_data  # type: ignore[attr-defined]
    entity._attr_available = True
    assert entity.available is False


def test_isy_entity_address_falls_back_to_dict_keys() -> None:
    """For a raw dict node (legacy program / variable record), ``address``
    falls back to ``dict.get('address', dict.get('id', ''))``."""
    from custom_components.udi_iox.entity import ISYEntity

    entity = ISYEntity.__new__(ISYEntity)
    entity._node = {"address": "0010"}  # type: ignore[attr-defined]
    assert entity._node_address() == "0010"
    entity._node = {"id": "var-id"}  # type: ignore[attr-defined]
    assert entity._node_address() == "var-id"


def test_isy_node_entity_async_on_update_marks_available_and_writes() -> None:
    """``ISYNodeEntity.async_on_update`` refreshes ``_attr_available``
    from ``node.enabled`` and triggers a state write."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._isy_data = isy_data  # type: ignore[attr-defined]
    entity._node = node  # type: ignore[attr-defined]
    entity._attr_available = False  # type: ignore[attr-defined]
    write_calls: list[int] = []
    with patch.object(
        ISYNodeEntity, "async_write_ha_state", lambda s: write_calls.append(1)
    ):
        entity.async_on_update(None, "")  # type: ignore[arg-type]
    assert entity._attr_available == node.enabled
    assert write_calls == [1]


def test_isy_program_entity_async_on_update_writes_state() -> None:
    """``ISYProgramEntity._on_program_status`` calls
    ``async_write_ha_state``."""
    from custom_components.udi_iox.entity import ISYProgramEntity

    entity = ISYProgramEntity.__new__(ISYProgramEntity)
    write_calls: list[int] = []
    with patch.object(
        ISYProgramEntity, "async_write_ha_state", lambda s: write_calls.append(1)
    ):
        # The handler signature accepts a single event arg.
        entity._on_program_status(MagicMock())  # type: ignore[arg-type]
    assert write_calls == [1]


def test_isy_node_entity_lifecycle_handler_filters_other_addresses() -> None:
    """``_on_lifecycle`` ignores frames for unrelated addresses /
    actions."""
    from pyisyox import NodeLifecycleAction, NodeLifecycleEvent

    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._isy_data = isy_data  # type: ignore[attr-defined]
    entity._node = node  # type: ignore[attr-defined]
    entity._attr_available = False  # type: ignore[attr-defined]
    write_calls: list[int] = []
    with patch.object(
        ISYNodeEntity, "async_write_ha_state", lambda s: write_calls.append(1)
    ):
        # Wrong address.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED,
                node_address="OTHER",
                raw_action="NE",
                seqnum=1,
            )
        )
        # Right address, wrong action.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_RENAMED,
                node_address="A 1",
                raw_action="NN",
                seqnum=2,
            )
        )
        # Match.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED,
                node_address="A 1",
                raw_action="NE",
                seqnum=3,
            )
        )
    assert write_calls == [1]


def test_aux_command_name_uses_nodedef_command_name() -> None:
    """An aux entity whose ``control`` is a command (not a property) —
    e.g. a plugin's ``SETOL`` setter — takes its name from the
    controller-published ``cmds.accepts[].name`` ("Set On Level"),
    not a title-cased id ("Setol"). Regression for a virtualgeneric
    node-server node."""
    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeCommands, NodeDef

    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("n001_1", "Virtual Generic"), controller)
    isy_data = isy_data_for(controller)
    nodedef = NodeDef(
        id="virtualgeneric",
        family_id="1",
        instance_id="1",
        cmds=NodeCommands(accepts=[Command(id="SETOL", name="Set On Level")]),
    )
    with patch.object(
        type(node), "nodedef", new_callable=lambda: property(lambda _s: nodedef)
    ):
        entity = ISYNodeEntity(isy_data, node, control="SETOL", unique_id="x_setol")
    assert entity._attr_name == "Set On Level"


def test_aux_command_name_falls_back_when_nodedef_unnamed() -> None:
    """A command absent from the nodedef (or with an empty name) still
    falls back to the COMMAND_FRIENDLY_NAME / title-cased path so an
    unresolved nodedef doesn't blank the entity name."""
    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("n001_1", "Virtual Generic"), controller)
    isy_data = isy_data_for(controller)
    # node.nodedef is None (no profile entry for this fixture) → fallback.
    entity = ISYNodeEntity(isy_data, node, control="SETOL", unique_id="x_setol")
    assert entity._attr_name == "Setol"


async def test_isy_entity_get_zwave_parameter_translates_node_command_error() -> None:
    """``async_get_zwave_parameter`` wraps NodeCommandError in
    HomeAssistantError."""
    from unittest.mock import AsyncMock

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NodeCommandError

    from custom_components.udi_iox.entity import ISYNodeEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "X"), controller)
    entity = ISYNodeEntity.__new__(ISYNodeEntity)
    entity._node = node  # type: ignore[attr-defined]
    with (
        patch.object(
            type(node),
            "get_zwave_parameter",
            new=AsyncMock(side_effect=NodeCommandError("nope")),
        ),
        pytest.raises(HomeAssistantError, match="nope"),
    ):
        await entity.async_get_zwave_parameter(1)
