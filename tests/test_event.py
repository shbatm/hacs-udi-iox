"""Snapshot tests for the udi_iox event platform."""

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
    return [Platform.EVENT]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_event_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every event entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


# --- Direct entity tests (cover event.py logic) ---


def _build_event_entity(controller, **node_record_kwargs):
    """Build a fully-wired ISYButtonEvent for direct unit testing."""
    from pyisyox.schema.cmd import Command
    from pyisyox.testing import make_node, make_node_record

    from custom_components.udi_iox.event import ISYButtonEvent

    record = make_node_record("AA AA AA 1", "Button", **node_record_kwargs)
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    triggers = [Command(id="DON"), Command(id="DOF")]
    return ISYButtonEvent(isy_data, node=node, triggers=triggers, device_info=None)


async def test_event_on_control_fires_matching_event_type() -> None:
    """A DON/DOF control fires the matching event_type via _trigger_event."""
    from unittest.mock import patch

    from pyisyox import Event
    from pyisyox.testing import make_controller, make_load_result

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller)

    fired: list = []
    with (
        patch.object(
            type(entity),
            "_trigger_event",
            lambda self, ev, attrs=None: fired.append((ev, attrs)),
        ),
        patch.object(type(entity), "async_write_ha_state", lambda self: None),
    ):
        evt = Event(
            seqnum=0,
            timestamp="",
            control="DON",
            action="",
            node_address="AA AA AA 1",
            event_info="payload",
        )
        entity._on_control(evt)
    assert len(fired) == 1
    _, attrs = fired[0]
    assert attrs == {"event_info": "payload"}


async def test_event_on_control_ignores_unknown_verb() -> None:
    """A control event not present in cmds.sends is ignored."""
    from unittest.mock import patch

    from pyisyox import Event
    from pyisyox.testing import make_controller, make_load_result

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller)

    fired: list = []
    with (
        patch.object(
            type(entity),
            "_trigger_event",
            lambda self, ev, attrs=None: fired.append(ev),
        ),
        patch.object(type(entity), "async_write_ha_state", lambda self: None),
    ):
        evt = Event(
            seqnum=0,
            timestamp="",
            control="UNKNOWN",
            action="",
            node_address="AA AA AA 1",
        )
        entity._on_control(evt)
    assert fired == []


async def test_event_on_control_suppressed_until_stream_live() -> None:
    """While pyisyox is replaying initial status (stream_live False),
    a matching control must NOT fire — the spurious-on-(re)connect
    guard. Once the replay drains (stream_live True) it fires."""
    from unittest.mock import patch

    from pyisyox import Event
    from pyisyox.testing import make_controller, make_load_result

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller)
    ce = entity._isy_data.controller_events
    ce.stream_live = False

    evt = Event(
        seqnum=0,
        timestamp="",
        control="DON",
        action="",
        node_address="AA AA AA 1",
    )
    fired: list = []
    with (
        patch.object(
            type(entity),
            "_trigger_event",
            lambda self, ev, attrs=None: fired.append(ev),
        ),
        patch.object(type(entity), "async_write_ha_state", lambda self: None),
    ):
        entity._on_control(evt)  # during replay → dropped
        assert fired == []
        ce.stream_live = True
        entity._on_control(evt)  # replay drained → fires
    assert len(fired) == 1


async def test_event_on_lifecycle_only_acts_on_node_enabled_for_this_address() -> None:
    """The lifecycle handler ignores events for other nodes and verbs
    that aren't NODE_ENABLED."""
    from dataclasses import replace
    from unittest.mock import patch

    from pyisyox import NodeLifecycleAction, NodeLifecycleEvent
    from pyisyox.testing import make_controller, make_load_result, make_node

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller)
    entity._attr_available = True

    with patch.object(type(entity), "async_write_ha_state", lambda self: None):
        # Other address → ignored; availability stays True.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED.value,
                node_address="OTHER",
                raw_action=NodeLifecycleAction.NODE_ENABLED.value,
                seqnum=0,
            )
        )
        assert entity._attr_available is True

        # Other verb → ignored; availability stays True.
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action="NN",
                node_address="AA AA AA 1",
                raw_action="NN",
                seqnum=0,
            )
        )
        assert entity._attr_available is True

        # Matching enable verb → updates availability from the node's
        # current enabled flag. Simulate the dispatcher mutating the
        # record by rebuilding the node off a disabled copy.
        entity._node = make_node(
            replace(entity._node._record, enabled=False), controller
        )
        entity._on_lifecycle(
            NodeLifecycleEvent(
                action=NodeLifecycleAction.NODE_ENABLED.value,
                node_address="AA AA AA 1",
                raw_action=NodeLifecycleAction.NODE_ENABLED.value,
                seqnum=0,
            )
        )
    assert entity._attr_available is False


# --- button_status attribute (#85, option (b)) ---


def test_event_button_status_omitted_when_node_reports_no_st() -> None:
    """A node with no ST property (plain pushbutton, motion / doorbell
    plugin) gets no button_status attribute at all -- not a misleading
    ``None``."""
    from pyisyox.testing import make_controller, make_load_result

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller, properties={})
    assert entity.extra_state_attributes == {}


def test_event_button_status_reports_raw_level_not_a_bool() -> None:
    """A KeypadLinc button wired to a fade up/down load reports its
    current dim level, not a coerced on/off bool. ``Node.status``
    normalizes the wire byte (0-255) to this nodedef's dimmable-editor
    percentage -- 128/255 -> 50 -- same normalization every other
    dimmable entity in this integration reads through."""
    from pyisyox.testing import make_controller, make_load_result

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller, status_value="128")
    assert entity.extra_state_attributes == {"button_status": 50}


async def test_event_on_control_status_refreshes_regardless_of_stream_live() -> None:
    """An ST control refreshes button_status via async_write_ha_state
    unconditionally -- unlike a button press, a stale reading catching
    up during the post-connect replay is exactly the point, not a
    spurious fire. It must not touch _trigger_event."""
    from unittest.mock import patch

    from pyisyox import Event
    from pyisyox.testing import make_controller, make_load_result

    controller = make_controller(make_load_result())
    entity = _build_event_entity(controller)
    entity._isy_data.controller_events.stream_live = False

    fired: list = []
    written = []
    with (
        patch.object(
            type(entity),
            "_trigger_event",
            lambda self, ev, attrs=None: fired.append(ev),
        ),
        patch.object(
            type(entity), "async_write_ha_state", lambda self: written.append(1)
        ),
    ):
        evt = Event(
            seqnum=0,
            timestamp="",
            control="ST",
            action="0",
            node_address="AA AA AA 1",
        )
        entity._on_control(evt)
    assert fired == []
    assert written == [1]
