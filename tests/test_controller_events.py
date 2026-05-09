"""Tests for IsyControllerEvents — the per-entity dispatch registry.

The registry is the central seam every entity hits, so these tests
pin: per-(address, control) routing, wildcard routing, lifecycle
fan-out, variable event payload extraction, unsubscribe semantics,
and exception isolation between listeners.

The registry is exercised against an in-process FakeController; the
production code under test is the dispatch logic itself, not pyisyox.
A separate test file exercises pyisyox's parser directly against
real captured fixtures.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.udi_iox.controller_events import IsyControllerEvents
from custom_components.udi_iox.models import IsyData


@pytest.fixture
def isy_data(fake_controller):
    """Build a minimally-populated IsyData wired to the FakeController."""
    data = IsyData()
    data.root = fake_controller
    return data


@pytest.fixture
def events(hass, isy_data):
    """Build the dispatch registry under test."""
    return IsyControllerEvents(hass, isy_data)


# --- subscribe_node ---------------------------------------------------


def test_subscribe_node_with_specific_control_routes_only_that_control(
    events, fake_controller, fake_event_factory
):
    """A listener registered for (addr, "ST") fires on ST events for
    that address and nothing else."""
    received: list = []
    events.subscribe_node("1A 2B 3C 1", "ST", received.append)

    fake_controller.fire_event(
        fake_event_factory(node_address="1A 2B 3C 1", control="ST", action="100")
    )
    fake_controller.fire_event(
        fake_event_factory(node_address="1A 2B 3C 1", control="OL", action="200")
    )
    fake_controller.fire_event(
        fake_event_factory(node_address="other", control="ST", action="300")
    )

    assert len(received) == 1
    assert received[0].action == "100"


def test_subscribe_node_with_none_control_acts_as_wildcard(
    events, fake_controller, fake_event_factory
):
    """control=None matches every control on the address."""
    received: list = []
    events.subscribe_node("addr", None, received.append)

    fake_controller.fire_event(fake_event_factory(node_address="addr", control="ST"))
    fake_controller.fire_event(fake_event_factory(node_address="addr", control="OL"))
    fake_controller.fire_event(fake_event_factory(node_address="other", control="ST"))

    assert len(received) == 2


def test_subscribe_node_specific_and_wildcard_both_fire(
    events, fake_controller, fake_event_factory
):
    """When a specific (addr, control) listener and a wildcard
    (addr, None) listener are both registered, both fire."""
    specific: list = []
    wildcard: list = []
    events.subscribe_node("addr", "ST", specific.append)
    events.subscribe_node("addr", None, wildcard.append)

    fake_controller.fire_event(fake_event_factory(node_address="addr", control="ST"))

    assert len(specific) == 1
    assert len(wildcard) == 1


def test_subscribe_node_unsubscribe_idempotent(
    events, fake_controller, fake_event_factory
):
    """Calling the returned unsubscribe twice doesn't raise; the
    second call is a no-op."""
    received: list = []
    unsub = events.subscribe_node("addr", "ST", received.append)
    unsub()
    unsub()  # idempotent — no exception

    fake_controller.fire_event(fake_event_factory(node_address="addr", control="ST"))
    assert received == []


def test_unsubscribing_last_listener_evicts_bucket(events):
    """When the last listener for a key unsubscribes, the dispatch
    bucket itself is dropped — otherwise reload-add-reload cycles
    would grow the dict unboundedly."""
    unsub_1 = events.subscribe_node("addr", "ST", lambda _e: None)
    unsub_2 = events.subscribe_node("addr", "ST", lambda _e: None)

    unsub_1()
    assert ("addr", "ST") in events._node_listeners  # one left
    unsub_2()
    assert ("addr", "ST") not in events._node_listeners


def test_subscribe_node_listener_exception_does_not_break_others(
    events, fake_controller, fake_event_factory
):
    """One listener raising must not prevent later listeners on the
    same key from firing."""
    other: list = []

    def raises(_event):
        raise RuntimeError("synthetic")

    events.subscribe_node("addr", "ST", raises)
    events.subscribe_node("addr", "ST", other.append)

    fake_controller.fire_event(fake_event_factory(node_address="addr", control="ST"))

    assert len(other) == 1


def test_subscribe_node_skips_events_with_empty_node_address(
    events, fake_controller, fake_event_factory
):
    """System events (no node_address) shouldn't be routed via the
    per-address registry."""
    received: list = []
    events.subscribe_node("", None, received.append)

    fake_controller.fire_event(
        fake_event_factory(node_address="", control="_5", action="0")
    )
    assert received == []


# --- subscribe_lifecycle ----------------------------------------------


def test_subscribe_lifecycle_fires_for_all_addresses(
    events, fake_controller, fake_lifecycle_factory
):
    """Lifecycle subscribers receive every NodeLifecycleEvent — the
    consumer filters on address inside the callback if needed."""
    received: list = []
    events.subscribe_lifecycle(received.append)

    fake_controller.fire_lifecycle(
        fake_lifecycle_factory(action="ND", node_address="aa")
    )
    fake_controller.fire_lifecycle(
        fake_lifecycle_factory(action="NR", node_address="bb")
    )

    assert len(received) == 2


def test_subscribe_lifecycle_unsubscribe_removes(
    events, fake_controller, fake_lifecycle_factory
):
    received: list = []
    unsub = events.subscribe_lifecycle(received.append)
    unsub()

    fake_controller.fire_lifecycle(
        fake_lifecycle_factory(action="ND", node_address="aa")
    )
    assert received == []


# --- subscribe_variable -----------------------------------------------


def test_subscribe_variable_value_change(events, fake_controller, fake_event_factory):
    """control=_1 action=6 with a <var><val> payload fires the
    listener with (value, None)."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fake_controller.fire_event(
        fake_event_factory(
            node_address="",
            control="_1",
            action="6",
            event_info='<var type="1" id="1"><prec>1</prec><val>20</val></var>',
        )
    )

    assert received == [(20, None)]


def test_subscribe_variable_init_change(events, fake_controller, fake_event_factory):
    """control=_1 action=7 with a <var><init> payload fires with
    (None, init)."""
    received: list = []
    events.subscribe_variable("1", "1", lambda v, i: received.append((v, i)))

    fake_controller.fire_event(
        fake_event_factory(
            node_address="",
            control="_1",
            action="7",
            event_info='<var type="1" id="1"><init>42</init><prec>0</prec></var>',
        )
    )

    assert received == [(None, 42)]


def test_subscribe_variable_filters_by_type_and_id(
    events, fake_controller, fake_event_factory
):
    """A listener for (1, 5) doesn't fire when type 2 / id 5 changes."""
    received: list = []
    events.subscribe_variable(1, 5, lambda v, i: received.append((v, i)))

    fake_controller.fire_event(
        fake_event_factory(
            node_address="",
            control="_1",
            action="6",
            event_info='<var type="2" id="5"><val>1</val></var>',
        )
    )
    assert received == []


def test_subscribe_variable_no_op_when_event_info_empty(
    events, fake_controller, fake_event_factory
):
    """Pre-pyisyox-PR-58 builds parse Event without event_info; the
    field is missing/empty and dispatch should silently no-op."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fake_controller.fire_event(
        fake_event_factory(
            node_address="", control="_1", action="6", event_info=""
        )
    )
    assert received == []


def test_subscribe_variable_handles_unparseable_payload(
    events, fake_controller, fake_event_factory
):
    """Garbage in event_info doesn't raise; the listener simply doesn't
    fire."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fake_controller.fire_event(
        fake_event_factory(
            node_address="",
            control="_1",
            action="6",
            event_info="not even xml",
        )
    )
    assert received == []


def test_subscribe_variable_skips_program_actions_on_underscore_one(
    events, fake_controller, fake_event_factory
):
    """The _1 control is shared with program events (action 0 and 3).
    Variable dispatch is gated on actions 6 and 7 specifically."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fake_controller.fire_event(
        fake_event_factory(
            node_address="",
            control="_1",
            action="0",
            event_info='<id>3</id>',
        )
    )
    assert received == []


# --- stop -------------------------------------------------------------


def test_stop_unsubscribes_from_controller(events, fake_controller, fake_event_factory):
    """After stop(), no more events should reach any registered listener."""
    received: list = []
    events.subscribe_node("addr", "ST", received.append)

    events.stop()

    fake_controller.fire_event(fake_event_factory(node_address="addr", control="ST"))
    assert received == []


def test_stop_clears_registries(events):
    """stop() drops listener references so reload doesn't leak."""
    events.subscribe_node("a", None, MagicMock())
    events.subscribe_lifecycle(MagicMock())
    events.subscribe_variable(1, 1, MagicMock())

    events.stop()

    assert events._node_listeners == {}
    assert events._lifecycle_listeners == []
    assert events._variable_listeners == {}


# --- HA bus event firing ----------------------------------------------


async def test_on_event_fires_udi_iox_control_on_bus(
    hass, events, fake_controller, fake_event_factory
):
    """Property events also fan out to the HA bus as udi_iox_control —
    the legacy automation surface that downstream automations key on."""
    fired: list = []
    hass.bus.async_listen("udi_iox_control", lambda evt: fired.append(evt.data))

    fake_controller.fire_event(
        fake_event_factory(node_address="addr", control="ST", action="100")
    )
    await hass.async_block_till_done()

    assert len(fired) == 1
    assert fired[0]["control"] == "ST"
    assert fired[0]["action"] == "100"
    assert fired[0]["node_address"] == "addr"
