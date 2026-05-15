"""Snapshot tests for the udi_iox binary_sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyisyox.testing import (
    make_controller,
    make_insteon_binary_sensor_records,
    make_load_result,
    make_node_record,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
)

from custom_components.udi_iox.binary_sensor import (
    ISYBinarySensorEntity,
    ISYBinarySensorHeartbeat,
    ISYInsteonBinarySensorEntity,
    async_setup_entry,
)
from tests.conftest import isy_data_for


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.BINARY_SENSOR]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_binary_sensor_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every binary_sensor entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


# --- Direct entity tests (lift binary_sensor.py coverage) ---


async def test_detect_device_type_and_class_insteon_matches_known_prefix() -> None:
    """An Insteon type matching one of BINARY_SENSOR_DEVICE_TYPES_ISY's
    prefixes returns its device_class + the raw type string."""
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import _detect_device_type_and_class

    controller = make_controller(make_load_result())
    # Type 16.x.x.x is Insteon motion (TYPE_INSTEON_MOTION starts with "16.").
    record = make_node_record("A 1", "Motion", type_="16.1.0.0", family_id="1")
    node = make_node(record, controller)
    device_class, device_type = _detect_device_type_and_class(node)
    assert device_class == BinarySensorDeviceClass.MOTION
    assert device_type == "16.1.0.0"


async def test_detect_device_type_and_class_unknown_returns_none() -> None:
    """An unrecognised Insteon type returns (None, type)."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import _detect_device_type_and_class

    controller = make_controller(make_load_result())
    record = make_node_record("A 1", "Mystery", type_="99.99.99.99")
    node = make_node(record, controller)
    device_class, device_type = _detect_device_type_and_class(node)
    assert device_class is None
    assert device_type == "99.99.99.99"


async def test_binary_sensor_entity_basic_is_on() -> None:
    """ISYBinarySensorEntity.is_on falls back to ``bool(value)``: ``None``
    → ``None`` (unknown), and every other value goes through Python's
    truthiness — so ``0`` → False and ``1`` → True. The "string 0 is
    truthy" wrinkle isn't relevant here because the wire-shape
    classifier only constructs raw-int ``ST`` for the binary-sensor
    family; the string case is exercised in the wire-coerce tests
    over in ``test_helpers.py``."""
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import ISYBinarySensorEntity

    controller = make_controller(make_load_result())
    # value=None → is_on None; value=1 → True; value=0 → False.
    cases: list[tuple[object, bool | None]] = [(None, None), (1, True), (0, False)]
    for value, expected in cases:
        record = make_node_record(
            "A 1",
            "Door",
            properties={
                "ST": NodePropertyValue(
                    id="ST",
                    value=value,
                    formatted=str(value),
                    uom="2",
                    name="Status",
                )
            },
        )
        node = make_node(record, controller)
        isy_data = isy_data_for(controller)
        entity = ISYBinarySensorEntity(
            isy_data,
            node=node,
            control="ST",
            unique_id="x",
            device_info=None,
        )
        assert entity.is_on is expected, f"value={value}"


async def test_insteon_binary_sensor_handlers_drive_state() -> None:
    """Positive node CMD_ON / CMD_OFF + negative node CMD_ON each set
    the computed state correctly. is_on returns the raw value for
    non-inverted classes."""
    from unittest.mock import patch

    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from pyisyox import Event
    from pyisyox.constants import CMD_OFF, CMD_ON
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import (
        ISYInsteonBinarySensorEntity,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Door"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYInsteonBinarySensorEntity(
        isy_data,
        node=node,
        device_class=BinarySensorDeviceClass.OPENING,
        device_info=None,
    )

    def _evt(control: str) -> Event:
        return Event(
            seqnum=0, timestamp="", control=control, action="", node_address="A 1"
        )

    with patch.object(
        ISYInsteonBinarySensorEntity, "async_write_ha_state", lambda s: None
    ):
        entity._async_positive_node_control_handler(_evt(CMD_ON))
        assert entity.is_on is True
        entity._async_positive_node_control_handler(_evt(CMD_OFF))
        assert entity.is_on is False
        entity._async_negative_node_control_handler(_evt(CMD_ON))
        assert entity.is_on is False  # negative DON = sensor goes Off


async def test_insteon_binary_sensor_inverts_for_light_and_moisture() -> None:
    """LIGHT and MOISTURE classes invert the computed state — primary
    DON means "dry"/"light detected"."""
    from unittest.mock import patch

    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from pyisyox import Event
    from pyisyox.constants import CMD_ON
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import (
        ISYInsteonBinarySensorEntity,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("A 1", "Leak"), controller)
    isy_data = isy_data_for(controller)
    for cls in (BinarySensorDeviceClass.MOISTURE, BinarySensorDeviceClass.LIGHT):
        entity = ISYInsteonBinarySensorEntity(
            isy_data, node=node, device_class=cls, device_info=None
        )
        with patch.object(
            ISYInsteonBinarySensorEntity, "async_write_ha_state", lambda s: None
        ):
            entity._async_positive_node_control_handler(
                Event(
                    seqnum=0,
                    timestamp="",
                    control=CMD_ON,
                    action="",
                    node_address="A 1",
                )
            )
        # is_on inverts → False even though _computed_state is True
        assert entity.is_on is False


async def test_insteon_binary_sensor_negative_node_attachment() -> None:
    """add_negative_node stores the subnode; add_heartbeat_device wires
    the heartbeat partner. _async_heartbeat forwards if attached."""
    from unittest.mock import MagicMock

    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import (
        ISYBinarySensorHeartbeat,
        ISYInsteonBinarySensorEntity,
    )

    controller = make_controller(make_load_result())
    main = make_node(make_node_record("A 1", "Door"), controller)
    sub = make_node(
        make_node_record("A 2", "Negative", parent_address="A 1"), controller
    )
    isy_data = isy_data_for(controller)
    entity = ISYInsteonBinarySensorEntity(
        isy_data,
        node=main,
        device_class=BinarySensorDeviceClass.OPENING,
        device_info=None,
    )
    entity.add_negative_node(sub)
    assert entity._negative_node is sub

    hb = MagicMock(spec=ISYBinarySensorHeartbeat)
    entity.add_heartbeat_device(hb)
    entity._async_heartbeat()
    hb.async_heartbeat.assert_called_once()


async def test_insteon_binary_sensor_on_update_recovers_unknown() -> None:
    """If the entity was initialised with unknown state and the node
    status arrives, async_on_update populates the computed state."""
    from dataclasses import replace
    from unittest.mock import patch

    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import (
        ISYInsteonBinarySensorEntity,
    )

    controller = make_controller(make_load_result())
    unknown = NodePropertyValue(
        id="ST", value=None, formatted="?", uom="2", name="Status"
    )
    known = NodePropertyValue(
        id="ST", value="1", formatted="On", uom="2", name="Status"
    )
    record = make_node_record("A 1", "Leak", properties={"ST": unknown})
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYInsteonBinarySensorEntity(
        isy_data,
        node=node,
        device_class=BinarySensorDeviceClass.OPENING,
        device_info=None,
    )
    # Simulate the dispatcher landing a status update — rebuild the node
    # off a fresh record (what the dispatcher does internally) and let
    # the entity re-read it.
    node = make_node(replace(record, properties={"ST": known}), controller)
    entity._node = node
    with patch.object(
        ISYInsteonBinarySensorEntity, "async_write_ha_state", lambda s: None
    ):
        entity.async_on_update(None, "")  # type: ignore[arg-type]
    assert entity.is_on is True


async def test_binary_sensor_program_entity_reads_status() -> None:
    """Program-driven binary sensors expose the status program's bool."""
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.binary_sensor import (
        ISYBinarySensorProgramEntity,
    )

    controller = make_controller(make_load_result())
    status = Program(
        make_program_record("0001", "Status", status=True), controller._client
    )
    isy_data = isy_data_for(controller)
    entity = ISYBinarySensorProgramEntity(isy_data, "Door", status)
    assert entity.is_on is True


# --- async_setup_entry coverage (Insteon parent / subnode classification) ---


def _build_isy_data(controller):
    """Build an ``IsyData`` with every controller node pinned onto
    ``Platform.BINARY_SENSOR``.

    The live classifier routes pure-Insteon-fixture nodes (no nodedef
    profile loaded) onto ``Platform.EVENT`` rather than BINARY_SENSOR,
    so we bypass it here and exercise ``async_setup_entry``'s subnode
    classification path directly — which is what we're trying to cover.
    """
    isy_data = isy_data_for(controller)
    isy_data.nodes[Platform.BINARY_SENSOR] = list(controller.nodes.values())
    return isy_data


def _make_entry(isy_data) -> MagicMock:
    entry = MagicMock()
    entry.runtime_data = isy_data
    return entry


@pytest.fixture
def insteon_binary_sensor_controller():
    """Controller seeded with every Insteon binary-sensor family from
    pyisyox.testing — exercises the full subnode-classification path
    in ``binary_sensor.async_setup_entry``."""
    return make_controller(make_load_result(nodes=make_insteon_binary_sensor_records()))


async def test_async_setup_entry_classifies_insteon_subnodes(
    insteon_binary_sensor_controller, hass
) -> None:
    """Every documented Insteon binary-sensor subnode is routed to the
    expected entity class + device class. Drives lines 98-219."""
    isy_data = _build_isy_data(insteon_binary_sensor_controller)
    entry = _make_entry(isy_data)
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)

    by_address = {e._node.address: e for e in collected}

    # --- leak sensor (16.8.x.x) ----------------------------------
    leak = by_address["30 30 30 1"]
    assert isinstance(leak, ISYInsteonBinarySensorEntity)
    assert leak.device_class == BinarySensorDeviceClass.MOISTURE
    leak_hb = by_address["30 30 30 4"]
    assert isinstance(leak_hb, ISYBinarySensorHeartbeat)

    # --- door sensor (16.9.x.x) ----------------------------------
    door = by_address["31 31 31 1"]
    assert isinstance(door, ISYInsteonBinarySensorEntity)
    assert door.device_class == BinarySensorDeviceClass.OPENING
    # Negative subnode is wired into the parent rather than fanned out.
    assert door._negative_node is not None
    assert door._negative_node.address == "31 31 31 2"
    assert "31 31 31 2" not in by_address

    # --- motion sensor (16.1.x.x) -------------------------------
    motion = by_address["32 32 32 1"]
    assert isinstance(motion, ISYInsteonBinarySensorEntity)
    assert motion.device_class == BinarySensorDeviceClass.MOTION
    # Dusk/Dawn subnode → its own LIGHT-class entity.
    dusk = by_address["32 32 32 2"]
    assert isinstance(dusk, ISYInsteonBinarySensorEntity)
    assert dusk.device_class == BinarySensorDeviceClass.LIGHT
    # Low battery subnode → BATTERY entity.
    low_batt = by_address["32 32 32 3"]
    assert low_batt.device_class == BinarySensorDeviceClass.BATTERY
    # Heartbeat subnode for motion has no dedicated branch (only the
    # OPENING / MOISTURE families wire a Heartbeat entity); subnode 4
    # falls through the generic-fallback branch as a plain entity.
    motion_hb = by_address["32 32 32 4"]
    assert type(motion_hb) is ISYBinarySensorEntity
    # Tamper subnode → PROBLEM entity.
    tamper = by_address["32 32 32 A"]
    assert tamper.device_class == BinarySensorDeviceClass.PROBLEM
    # Disabled subnode → bare entity (no device_class).
    disabled = by_address["32 32 32 D"]
    assert isinstance(disabled, ISYInsteonBinarySensorEntity)
    assert disabled.device_class is None

    # --- thermostat binary sensors (5.16.x.x) -------------------
    therm_cool = by_address["33 33 33 2"]
    assert therm_cool.device_class == BinarySensorDeviceClass.COLD
    therm_heat = by_address["33 33 33 3"]
    assert therm_heat.device_class == BinarySensorDeviceClass.HEAT


async def test_async_setup_entry_subnode_with_unknown_parent_logs_and_skips(
    hass, caplog
) -> None:
    """A child subnode that requires a parent (OPENING) but whose primary
    address is missing falls into the ``no device was created for it``
    branch (line 146-150)."""
    import logging

    # An OPENING-class child whose primary_address points at a node the
    # classifier rejected (e.g. ignored by name). We omit the primary
    # entirely to trigger the missing-parent path.
    orphan = make_node_record(
        "DE AD BE 2",
        "Orphan Negative",
        type_="16.9.1.0",
        pnode="DE AD BE 1",  # primary that doesn't exist in the controller
    )
    controller = make_controller(make_load_result(nodes={orphan.address: orphan}))
    isy_data = _build_isy_data(controller)
    entry = _make_entry(isy_data)
    collected: list = []
    with caplog.at_level(logging.ERROR):
        await async_setup_entry(hass, entry, collected.extend)
    assert any("no device was created for it" in r.message for r in caplog.records)


async def test_async_setup_entry_node_server_takes_non_insteon_branch(hass) -> None:
    """A non-Insteon node (here a node-server plugin node) routes through
    the simple ``ISYBinarySensorEntity`` constructor — bypassing the
    Insteon parent/child fan-out (line 111-113)."""
    record = make_node_record(
        "n001_test_1",
        "Plugin Sensor",
        type_="99.99.99.99",
        family_id="10",  # node_server protocol
    )
    controller = make_controller(make_load_result(nodes={record.address: record}))
    isy_data = _build_isy_data(controller)
    entry = _make_entry(isy_data)
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert len(collected) == 1
    assert isinstance(collected[0], ISYBinarySensorEntity)
    assert not isinstance(collected[0], ISYInsteonBinarySensorEntity)


async def test_async_setup_entry_skips_program_device_without_device_info(hass) -> None:
    """A program in ``program_devices`` whose DeviceInfo wasn't
    registered (line 226-227) is silently skipped — no entity created."""
    from pyisyox import Program
    from pyisyox.testing import make_program_record

    record = make_program_record("0010", "Sunset Lights", path="Lighting/Sunset Lights")
    controller = make_controller(make_load_result(programs={record.address: record}))
    isy_data = isy_data_for(controller)
    isy_data.program_devices = [Program(record, controller._client)]
    # Note: no isy_data.devices entry under "program_0010" — should skip.
    entry = _make_entry(isy_data)
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert collected == []


async def test_async_setup_entry_aux_property_creates_entity(hass) -> None:
    """A ``(node, control)`` pair in ``aux_properties[BINARY_SENSOR]``
    creates an ``ISYBinarySensorEntity`` keyed on the control suffix
    (lines 232-242)."""
    record = make_node_record("AA AA AA 1", "Aux", type_="1.0.0.0")
    controller = make_controller(make_load_result(nodes={record.address: record}))
    node = controller.nodes[record.address]
    isy_data = isy_data_for(controller)
    isy_data.aux_properties[Platform.BINARY_SENSOR].append((node, "DOF"))
    entry = _make_entry(isy_data)
    collected: list = []
    await async_setup_entry(hass, entry, collected.extend)
    assert len(collected) == 1
    e = collected[0]
    assert isinstance(e, ISYBinarySensorEntity)
    assert e.unique_id.endswith("_DOF")


async def test_detect_device_type_handles_missing_type_attr() -> None:
    """If ``node.type`` raises AttributeError, the detector returns
    ``(None, None)`` (line 251-253)."""
    from custom_components.udi_iox.binary_sensor import _detect_device_type_and_class

    class _NoType:
        @property
        def type(self):
            raise AttributeError("nope")

    device_class, device_type = _detect_device_type_and_class(_NoType())  # type: ignore[arg-type]
    assert device_class is None
    assert device_type is None


async def test_insteon_binary_sensor_is_on_returns_none_when_unknown() -> None:
    """Insteon entity exposes None when ``_computed_state`` is None
    (line 458-459) — important for inverted classes so a None doesn't
    flip to True."""
    record = make_node_record("AA AA AA 1", "Leak", type_="16.8.1.0")
    controller = make_controller(make_load_result(nodes={record.address: record}))
    node = controller.nodes[record.address]
    isy_data = isy_data_for(controller)
    entity = ISYInsteonBinarySensorEntity(
        isy_data,
        node=node,
        device_class=BinarySensorDeviceClass.MOISTURE,
        unknown_state=None,
        device_info=None,
    )
    entity._computed_state = None
    assert entity.is_on is None


async def test_heartbeat_extra_state_attributes_includes_parent_id() -> None:
    """The heartbeat entity exposes ``parent_entity_id`` (line 585)."""
    main = make_node_record("AA AA AA 1", "Sensor", type_="16.1.1.0")
    sub = make_node_record(
        "AA AA AA 4", "Heartbeat", type_="16.1.1.0", pnode="AA AA AA 1"
    )
    controller = make_controller(
        make_load_result(nodes={main.address: main, sub.address: sub})
    )
    isy_data = isy_data_for(controller)
    parent = ISYInsteonBinarySensorEntity(
        isy_data,
        node=controller.nodes[main.address],
        device_class=BinarySensorDeviceClass.MOTION,
        device_info=None,
    )
    parent.entity_id = "binary_sensor.parent"
    hb = ISYBinarySensorHeartbeat(
        isy_data, node=controller.nodes[sub.address], parent_device=parent
    )
    assert hb.extra_state_attributes == {"parent_entity_id": "binary_sensor.parent"}


async def test_heartbeat_async_on_update_is_a_no_op() -> None:
    """``ISYBinarySensorHeartbeat.async_on_update`` intentionally does
    nothing — control events are the only signal it acts on."""
    main = make_node_record("AA AA AA 1", "Sensor", type_="16.1.1.0")
    sub = make_node_record(
        "AA AA AA 4", "Heartbeat", type_="16.1.1.0", pnode="AA AA AA 1"
    )
    controller = make_controller(
        make_load_result(nodes={main.address: main, sub.address: sub})
    )
    isy_data = isy_data_for(controller)
    parent = ISYInsteonBinarySensorEntity(
        isy_data,
        node=controller.nodes[main.address],
        device_class=BinarySensorDeviceClass.MOTION,
        device_info=None,
    )
    hb = ISYBinarySensorHeartbeat(
        isy_data, node=controller.nodes[sub.address], parent_device=parent
    )
    # No assertion — the method must not raise.
    hb.async_on_update(None, "")  # type: ignore[arg-type]


async def test_heartbeat_timer_cancels_previous_and_fires_after_25h(
    hass, freezer
) -> None:
    """Restarting the timer cancels the prior callback (line 543-545),
    and the elapsed-callback flips ``_computed_state`` to True
    (line 547-552 — Low Battery)."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    main = make_node_record("AA AA AA 1", "Sensor", type_="16.1.1.0")
    sub = make_node_record(
        "AA AA AA 4", "Heartbeat", type_="16.1.1.0", pnode="AA AA AA 1"
    )
    controller = make_controller(
        make_load_result(nodes={main.address: main, sub.address: sub})
    )
    isy_data = isy_data_for(controller)
    parent = ISYInsteonBinarySensorEntity(
        isy_data,
        node=controller.nodes[main.address],
        device_class=BinarySensorDeviceClass.MOTION,
        device_info=None,
    )
    hb = ISYBinarySensorHeartbeat(
        isy_data, node=controller.nodes[sub.address], parent_device=parent
    )
    hb.hass = hass
    hb.entity_id = "binary_sensor.hb"
    # Start the timer.
    hb._restart_timer()
    first_unsub = hb._heartbeat_timer
    assert first_unsub is not None
    # Restart cancels the prior callback (prev != None branch).
    hb._restart_timer()
    assert hb._heartbeat_timer is not first_unsub

    # Fire the 25h elapsed callback by jumping the clock past 25h.
    freezer.move_to(dt_util.utcnow() + timedelta(hours=26))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hb._computed_state is True
    assert hb._heartbeat_timer is None


async def test_insteon_async_added_to_hass_subscribes_both_nodes(hass) -> None:
    """``async_added_to_hass`` subscribes to the positive node always,
    and to the negative node when one is attached (lines 336-355)."""
    main = make_node_record("AA AA AA 1", "Door", type_="16.9.1.0")
    sub = make_node_record(
        "AA AA AA 2", "Negative", type_="16.9.1.0", pnode="AA AA AA 1"
    )
    controller = make_controller(
        make_load_result(nodes={main.address: main, sub.address: sub})
    )
    isy_data = isy_data_for(controller)

    sub_calls: list[tuple] = []

    def _capture(addr, control, cb):
        sub_calls.append((addr, control))
        return lambda: None

    isy_data.controller_events.subscribe_node = _capture  # type: ignore[assignment]
    isy_data.controller_events.subscribe_status = _capture  # type: ignore[assignment]

    entity = ISYInsteonBinarySensorEntity(
        isy_data,
        node=controller.nodes[main.address],
        device_class=BinarySensorDeviceClass.OPENING,
        device_info=None,
    )
    entity.add_negative_node(controller.nodes[sub.address])
    entity.hass = hass
    entity.entity_id = "binary_sensor.door"
    await entity.async_added_to_hass()
    addrs_seen = {addr for addr, _ in sub_calls}
    assert "AA AA AA 1" in addrs_seen
    assert "AA AA AA 2" in addrs_seen


async def test_heartbeat_async_added_to_hass_restores_low_battery(hass) -> None:
    """If the prior state was ON (Low Battery), ``async_added_to_hass``
    restores ``_computed_state`` to True (lines 503-518)."""
    from unittest.mock import AsyncMock

    from homeassistant.const import STATE_ON

    main = make_node_record("AA AA AA 1", "Sensor", type_="16.1.1.0")
    sub = make_node_record(
        "AA AA AA 4", "Heartbeat", type_="16.1.1.0", pnode="AA AA AA 1"
    )
    controller = make_controller(
        make_load_result(nodes={main.address: main, sub.address: sub})
    )
    isy_data = isy_data_for(controller)
    parent = ISYInsteonBinarySensorEntity(
        isy_data,
        node=controller.nodes[main.address],
        device_class=BinarySensorDeviceClass.MOTION,
        device_info=None,
    )
    hb = ISYBinarySensorHeartbeat(
        isy_data, node=controller.nodes[sub.address], parent_device=parent
    )
    hb.hass = hass
    hb.entity_id = "binary_sensor.hb_restore"
    last = MagicMock()
    last.state = STATE_ON
    hb.async_get_last_state = AsyncMock(return_value=last)
    await hb.async_added_to_hass()
    assert hb._computed_state is True
    # Cancel the timer so it doesn't linger past the test.
    if hb._heartbeat_timer is not None:
        hb._heartbeat_timer()


async def test_heartbeat_entity_handles_heartbeat_and_timer(hass) -> None:
    """A node-control event from either CMD_ON or CMD_OFF marks the
    heartbeat alive (computed_state=False = "Normal battery")."""
    from unittest.mock import patch

    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from pyisyox import Event
    from pyisyox.constants import CMD_ON
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.binary_sensor import (
        ISYBinarySensorHeartbeat,
        ISYInsteonBinarySensorEntity,
    )

    controller = make_controller(make_load_result())
    main = make_node(make_node_record("A 1", "Sensor"), controller)
    sub = make_node(make_node_record("A 4", "HB", parent_address="A 1"), controller)
    isy_data = isy_data_for(controller)
    parent = ISYInsteonBinarySensorEntity(
        isy_data,
        node=main,
        device_class=BinarySensorDeviceClass.MOTION,
        device_info=None,
    )
    hb = ISYBinarySensorHeartbeat(isy_data, node=sub, parent_device=parent)
    hb.hass = hass

    with patch.object(ISYBinarySensorHeartbeat, "async_write_ha_state", lambda s: None):
        hb._heartbeat_node_control_handler(
            Event(seqnum=0, timestamp="", control=CMD_ON, action="", node_address="A 4")
        )
        assert hb.is_on is False  # heartbeat alive = battery normal

        # Stale event types ignored.
        hb._heartbeat_node_control_handler(
            Event(
                seqnum=0,
                timestamp="",
                control="OTHER",
                action="",
                node_address="A 4",
            )
        )
        # Cancel the timer so it doesn't linger.
        if hb._heartbeat_timer is not None:
            hb._heartbeat_timer()
            hb._heartbeat_timer = None
