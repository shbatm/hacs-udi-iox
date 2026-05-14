"""Snapshot tests for the udi_iox binary_sensor platform."""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
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
