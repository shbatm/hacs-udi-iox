"""Tests for IsyControllerEvents — the per-entity dispatch registry.

The registry is the central seam every entity hits, so these tests
pin: per-(address, control) routing, wildcard routing, lifecycle
fan-out, variable event payload extraction, unsubscribe semantics,
and exception isolation between listeners.

Driven against a real :class:`pyisyox.Controller` (built via
``pyisyox.testing``); synthetic :class:`Event` / :class:`NodeLifecycleEvent`
instances are fanned out via the dispatcher's internal listener lists.
pyisyox's own suite covers parser → dispatcher correctness; here we
exercise the consumer's dispatch logic on top of the real wire shapes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir
from pyisyox import Event, NodeLifecycleEvent
from pyisyox.testing import (
    fire_event,
    fire_lifecycle,
    make_controller,
    make_load_result,
)
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.udi_iox.const import DOMAIN
from custom_components.udi_iox.controller_events import IsyControllerEvents
from custom_components.udi_iox.models import IsyData


def _event(
    *,
    node_address: str = "",
    control: str = "ST",
    action: str = "0",
    event_info: str = "",
    seqnum: int = 0,
) -> Event:
    """Build an :class:`Event` with sensible defaults — the registry
    keys on ``node_address`` / ``control`` / ``action`` so the other
    fields are usually irrelevant per test."""
    return Event(
        seqnum=seqnum,
        timestamp="",
        control=control,
        action=action,
        node_address=node_address,
        event_info=event_info,
    )


def _lifecycle(
    *, action: str, node_address: str, seqnum: int = 0
) -> NodeLifecycleEvent:
    """Build a :class:`NodeLifecycleEvent` keyed only on the verb +
    address (everything else is irrelevant for the consumer's dispatch
    + Repair logic)."""
    return NodeLifecycleEvent(
        action=action,
        node_address=node_address,
        raw_action=action,
        seqnum=seqnum,
    )


@pytest.fixture
def event_controller():
    """A real, empty :class:`Controller` shaped for dispatch tests.

    No nodes / programs are needed — every test synthesises its own
    events. The dispatcher's listener registry is what's exercised."""
    return make_controller(make_load_result())


@pytest.fixture
def isy_data(event_controller):
    """Build a minimally-populated IsyData wired to the Controller."""
    data = IsyData()
    data.root = event_controller
    return data


@pytest.fixture
def events(hass, isy_data):
    """Build the dispatch registry under test."""
    return IsyControllerEvents(hass, isy_data)


# --- subscribe_node ---------------------------------------------------


def test_subscribe_node_with_specific_control_routes_only_that_control(
    events, event_controller
):
    """A listener registered for (addr, "ST") fires on ST events for
    that address and nothing else."""
    received: list = []
    events.subscribe_node("1A 2B 3C 1", "ST", received.append)

    fire_event(
        event_controller,
        _event(node_address="1A 2B 3C 1", control="ST", action="100"),
    )
    fire_event(
        event_controller,
        _event(node_address="1A 2B 3C 1", control="OL", action="200"),
    )
    fire_event(
        event_controller, _event(node_address="other", control="ST", action="300")
    )

    assert len(received) == 1
    assert received[0].action == "100"


def test_subscribe_node_with_none_control_acts_as_wildcard(events, event_controller):
    """control=None matches every control on the address."""
    received: list = []
    events.subscribe_node("addr", None, received.append)

    fire_event(event_controller, _event(node_address="addr", control="ST"))
    fire_event(event_controller, _event(node_address="addr", control="OL"))
    fire_event(event_controller, _event(node_address="other", control="ST"))

    assert len(received) == 2


def test_subscribe_node_specific_and_wildcard_both_fire(events, event_controller):
    """When a specific (addr, control) listener and a wildcard
    (addr, None) listener are both registered, both fire."""
    specific: list = []
    wildcard: list = []
    events.subscribe_node("addr", "ST", specific.append)
    events.subscribe_node("addr", None, wildcard.append)

    fire_event(event_controller, _event(node_address="addr", control="ST"))

    assert len(specific) == 1
    assert len(wildcard) == 1


def test_subscribe_node_unsubscribe_idempotent(events, event_controller):
    """Calling the returned unsubscribe twice doesn't raise; the
    second call is a no-op."""
    received: list = []
    unsub = events.subscribe_node("addr", "ST", received.append)
    unsub()
    unsub()  # idempotent — no exception

    fire_event(event_controller, _event(node_address="addr", control="ST"))
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
    events, event_controller
):
    """One listener raising must not prevent later listeners on the
    same key from firing."""
    other: list = []

    def raises(_event):
        raise RuntimeError("synthetic")

    events.subscribe_node("addr", "ST", raises)
    events.subscribe_node("addr", "ST", other.append)

    fire_event(event_controller, _event(node_address="addr", control="ST"))

    assert len(other) == 1


def test_subscribe_node_skips_events_with_empty_node_address(events, event_controller):
    """System events (no node_address) shouldn't be routed via the
    per-address registry."""
    received: list = []
    events.subscribe_node("", None, received.append)

    fire_event(event_controller, _event(node_address="", control="_5", action="0"))
    assert received == []


# --- subscribe_lifecycle ----------------------------------------------


def test_subscribe_lifecycle_fires_for_all_addresses(events, event_controller):
    """Lifecycle subscribers receive every NodeLifecycleEvent — the
    consumer filters on address inside the callback if needed."""
    received: list = []
    events.subscribe_lifecycle(received.append)

    fire_lifecycle(event_controller, _lifecycle(action="ND", node_address="aa"))
    fire_lifecycle(event_controller, _lifecycle(action="NR", node_address="bb"))

    assert len(received) == 2


def test_subscribe_lifecycle_unsubscribe_removes(events, event_controller):
    received: list = []
    unsub = events.subscribe_lifecycle(received.append)
    unsub()

    fire_lifecycle(event_controller, _lifecycle(action="ND", node_address="aa"))
    assert received == []


# --- subscribe_variable -----------------------------------------------


def test_subscribe_variable_value_change(events, event_controller):
    """control=_1 action=6 with a <var><val> payload fires the
    listener with (value, None)."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fire_event(
        event_controller,
        _event(
            node_address="",
            control="_1",
            action="6",
            event_info='<var type="1" id="1"><prec>1</prec><val>20</val></var>',
        ),
    )

    assert received == [(20, None)]


def test_subscribe_variable_init_change(events, event_controller):
    """control=_1 action=7 with a <var><init> payload fires with
    (None, init)."""
    received: list = []
    events.subscribe_variable("1", "1", lambda v, i: received.append((v, i)))

    fire_event(
        event_controller,
        _event(
            node_address="",
            control="_1",
            action="7",
            event_info='<var type="1" id="1"><init>42</init><prec>0</prec></var>',
        ),
    )

    assert received == [(None, 42)]


def test_subscribe_variable_filters_by_type_and_id(events, event_controller):
    """A listener for (1, 5) doesn't fire when type 2 / id 5 changes."""
    received: list = []
    events.subscribe_variable(1, 5, lambda v, i: received.append((v, i)))

    fire_event(
        event_controller,
        _event(
            node_address="",
            control="_1",
            action="6",
            event_info='<var type="2" id="5"><val>1</val></var>',
        ),
    )
    assert received == []


def test_subscribe_variable_no_op_when_event_info_empty(events, event_controller):
    """Pre-pyisyox-PR-58 builds parse Event without event_info; the
    field is missing/empty and dispatch should silently no-op."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fire_event(
        event_controller,
        _event(node_address="", control="_1", action="6", event_info=""),
    )
    assert received == []


def test_subscribe_variable_handles_unparseable_payload(events, event_controller):
    """Garbage in event_info doesn't raise; the listener simply doesn't
    fire."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fire_event(
        event_controller,
        _event(
            node_address="",
            control="_1",
            action="6",
            event_info="not even xml",
        ),
    )
    assert received == []


def test_subscribe_variable_skips_program_actions_on_underscore_one(
    events, event_controller
):
    """The _1 control is shared with program events (action 0 and 3).
    Variable dispatch is gated on actions 6 and 7 specifically."""
    received: list = []
    events.subscribe_variable(1, 1, lambda v, i: received.append((v, i)))

    fire_event(
        event_controller,
        _event(
            node_address="",
            control="_1",
            action="0",
            event_info="<id>3</id>",
        ),
    )
    assert received == []


# --- stop -------------------------------------------------------------


def test_stop_unsubscribes_from_controller(events, event_controller):
    """After stop(), no more events should reach any registered listener."""
    received: list = []
    events.subscribe_node("addr", "ST", received.append)

    events.stop()

    fire_event(event_controller, _event(node_address="addr", control="ST"))
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


# --- lifecycle repair issue ------------------------------------------


@pytest.fixture
def events_with_entry(hass, isy_data):
    """Variant of the registry that knows its entry_id, so the
    lifecycle Repair flow has a target."""
    return IsyControllerEvents(hass, isy_data, entry_id="test-entry")


def _reload_issue(hass) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, "lifecycle_reload_required.test-entry"
    )


def test_reload_required_lifecycle_creates_repair_issue(
    hass, events_with_entry, event_controller
):
    """A reload-worthy verb (``ND``/``NR``/``NN``/``EN``/``RV``/``RG``)
    raises a Repair card so the user can trigger an entry reload."""
    fire_lifecycle(event_controller, _lifecycle(action="ND", node_address="aa"))

    issue = _reload_issue(hass)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.data == {"entry_id": "test-entry"}
    assert issue.translation_placeholders == {
        "verb": "Node Added",
        "address": "aa",
    }


def test_soft_lifecycle_does_not_create_repair_issue(
    hass, events_with_entry, event_controller
):
    """``MV``/``PC``/``WH``/``WD``/``CE``/``NE`` are informational and
    don't invalidate the cached node registry — no Repair card."""
    fire_lifecycle(event_controller, _lifecycle(action="NE", node_address="aa"))
    fire_lifecycle(event_controller, _lifecycle(action="MV", node_address="aa"))

    assert _reload_issue(hass) is None


def test_repeated_reload_lifecycle_coalesces(hass, events_with_entry, event_controller):
    """Two reload-worthy events while the card is up update the card
    in-place rather than spawning a second one — issue_id is keyed on
    entry_id, so async_create_issue is idempotent."""
    fire_lifecycle(event_controller, _lifecycle(action="ND", node_address="aa"))
    fire_lifecycle(event_controller, _lifecycle(action="NR", node_address="bb"))

    issue = _reload_issue(hass)
    assert issue is not None
    # Latest verb + address wins.
    assert issue.translation_placeholders == {
        "verb": "Node Removed",
        "address": "bb",
    }


def test_reload_lifecycle_skipped_when_no_entry_id(hass, events, event_controller):
    """Test fixtures that don't supply an entry_id (older suites,
    smoke tests) must still route lifecycle events without raising or
    creating a malformed Repair card."""
    fire_lifecycle(event_controller, _lifecycle(action="ND", node_address="aa"))
    assert _reload_issue(hass) is None


# --- WS-status / entity-unavailable ----------------------------------------


async def test_ws_disconnect_debounced_then_recovers_without_flip(hass, events):
    """A brief disconnect that recovers inside the 90 s window must NOT
    flip entities to unavailable — listeners stay silent, ws_connected
    stays True."""
    from pyisyox.constants import EventStreamStatus

    seen: list[bool] = []
    events.subscribe_ws_status(seen.append)
    assert events.ws_connected is True

    events._on_ws_status(EventStreamStatus.LOST_CONNECTION)
    # Timer scheduled, but ws_connected stays True until it fires.
    assert events.ws_connected is True
    assert seen == []

    events._on_ws_status(EventStreamStatus.CONNECTED)
    assert events.ws_connected is True
    # No transition ever surfaced — entities never went unavailable.
    assert seen == []


async def test_ws_disconnect_past_debounce_flips_unavailable(hass, events):
    """If the disconnect outlasts the 90 s window, the timer fires,
    ws_connected flips to False, and every listener is notified once."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pyisyox.constants import EventStreamStatus

    from custom_components.udi_iox.controller_events import (
        WS_UNAVAILABLE_DEBOUNCE_SECONDS,
    )

    seen: list[bool] = []
    events.subscribe_ws_status(seen.append)

    events._on_ws_status(EventStreamStatus.LOST_CONNECTION)
    assert events.ws_connected is True
    assert seen == []

    # Advance HA's clock past the debounce window.
    future = dt_util.utcnow() + timedelta(seconds=WS_UNAVAILABLE_DEBOUNCE_SECONDS + 1)
    async_fire_time_changed(hass, future)
    await hass.async_block_till_done()

    assert events.ws_connected is False
    assert seen == [False]

    # Subsequent reconnect flips back instantly.
    events._on_ws_status(EventStreamStatus.CONNECTED)
    assert events.ws_connected is True
    assert seen == [False, True]


async def test_ws_repeat_disconnect_frames_dont_reschedule_timer(hass, events):
    """Multiple non-CONNECTED frames during a slow reconnect mustn't
    push the unavailable deadline back — once the timer is armed it
    counts down without restarts."""
    from pyisyox.constants import EventStreamStatus

    events._on_ws_status(EventStreamStatus.LOST_CONNECTION)
    handle_1 = events._ws_disconnect_timer
    assert handle_1 is not None

    events._on_ws_status(EventStreamStatus.RECONNECTING)
    handle_2 = events._ws_disconnect_timer
    assert handle_2 is handle_1  # same cancel callable — not rearmed

    events.stop()  # avoid the lingering-timer guard


async def test_stop_cancels_pending_ws_disconnect_timer(hass, events):
    """``stop()`` must cancel any pending unavailable-flip timer —
    otherwise a config-entry reload would still trigger a stale
    unavailable signal seconds later against the new dispatcher."""
    from pyisyox.constants import EventStreamStatus

    seen: list[bool] = []
    events.subscribe_ws_status(seen.append)

    events._on_ws_status(EventStreamStatus.LOST_CONNECTION)
    assert events._ws_disconnect_timer is not None

    events.stop()
    assert events._ws_disconnect_timer is None

    # Advance the clock well past the debounce window — the cancelled
    # timer must NOT fire.
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.udi_iox.controller_events import (
        WS_UNAVAILABLE_DEBOUNCE_SECONDS,
    )

    future = dt_util.utcnow() + timedelta(seconds=WS_UNAVAILABLE_DEBOUNCE_SECONDS + 1)
    async_fire_time_changed(hass, future)
    await hass.async_block_till_done()

    assert seen == []  # listener cleared by stop(); no signal fanned out


# --- Coverage: unsubscribe idempotency + program/lifecycle exceptions + ws ---


def test_unsubscribing_lifecycle_listener_twice_is_safe(
    events, event_controller
) -> None:
    """Calling the unsubscriber a second time is a no-op
    (covers lines 187-188)."""
    cb = MagicMock()
    unsub = events.subscribe_lifecycle(cb)
    unsub()
    unsub()  # second call must not raise


def test_unsubscribing_variable_listener_twice_is_safe(
    events, event_controller
) -> None:
    """Variable unsubscribe is also idempotent (lines 222-223)."""
    cb = MagicMock()
    unsub = events.subscribe_variable(1, "1", cb)
    unsub()
    unsub()


def test_unsubscribing_program_listener_twice_is_safe(events, event_controller) -> None:
    """Program-status unsubscribe is idempotent (lines 254-255)."""
    cb = MagicMock()
    unsub = events.subscribe_program("0001", cb)
    unsub()
    unsub()


def test_unsubscribing_ws_status_listener_twice_is_safe(
    events, event_controller
) -> None:
    """WS-status unsubscribe is idempotent (lines 275-276)."""
    cb = MagicMock()
    unsub = events.subscribe_ws_status(cb)
    unsub()
    unsub()


def test_lifecycle_listener_exception_does_not_break_others(
    events, event_controller, caplog
) -> None:
    """A raising lifecycle listener is logged but doesn't block siblings
    (lines 427-428)."""
    import logging

    boom = MagicMock(side_effect=RuntimeError("nope"))
    after = MagicMock()
    events.subscribe_lifecycle(boom)
    events.subscribe_lifecycle(after)
    with caplog.at_level(logging.ERROR):
        fire_lifecycle(
            event_controller,
            _lifecycle(action="NE", node_address="A 1"),
        )
    boom.assert_called_once()
    after.assert_called_once()


def test_program_listener_exception_does_not_break_others(events, caplog) -> None:
    """A raising program listener is logged but doesn't block siblings
    (lines 438-443)."""
    import logging

    from pyisyox import ProgramStatusEvent

    boom = MagicMock(side_effect=RuntimeError("nope"))
    after = MagicMock()
    events.subscribe_program("0010", boom)
    events.subscribe_program("0010", after)
    evt = MagicMock(spec=ProgramStatusEvent)
    evt.address = "0010"
    with caplog.at_level(logging.ERROR):
        events._on_program_status(evt)
    boom.assert_called_once()
    after.assert_called_once()


def test_ws_status_listener_exception_does_not_break_others(events, caplog) -> None:
    """A raising ws-status listener is logged but doesn't block siblings
    (lines 519-520)."""
    import logging

    boom = MagicMock(side_effect=RuntimeError("nope"))
    after = MagicMock()
    events.subscribe_ws_status(boom)
    events.subscribe_ws_status(after)
    with caplog.at_level(logging.ERROR):
        events._fan_out_ws_status(True)
    boom.assert_called_once()
    after.assert_called_once()


def test_variable_wildcard_listener_exception_isolation(
    events, event_controller, caplog
) -> None:
    """A wildcard-variable listener exception doesn't block other
    listeners (lines 348-349)."""
    import logging

    boom = MagicMock(side_effect=RuntimeError("nope"))
    after = MagicMock()
    events.subscribe_variable(1, "1", boom)
    events.subscribe_variable(1, "1", after)
    with caplog.at_level(logging.ERROR):
        fire_event(
            event_controller,
            _event(
                control="_1",
                action="6",
                event_info='<var type="1" id="1"><val>5</val></var>',
            ),
        )
    boom.assert_called_once()
    after.assert_called_once()


def test_variable_event_with_missing_var_element_is_dropped(
    events, event_controller
) -> None:
    """An event_info that parses but has no ``<var>`` element is
    silently dropped (line 376)."""
    cb = MagicMock()
    events.subscribe_variable(1, "1", cb)
    fire_event(
        event_controller,
        _event(control="_1", action="6", event_info="<other/>"),
    )
    cb.assert_not_called()


def test_variable_event_with_missing_type_or_id_is_dropped(
    events, event_controller
) -> None:
    """A ``<var>`` element without ``type`` / ``id`` is dropped
    (lines 385-386)."""
    cb = MagicMock()
    events.subscribe_variable(1, "1", cb)
    fire_event(
        event_controller,
        _event(control="_1", action="6", event_info="<var><val>5</val></var>"),
    )
    cb.assert_not_called()


def test_variable_event_with_unparseable_value_is_dropped(
    events, event_controller
) -> None:
    """A ``<val>`` payload that isn't an int is dropped
    (lines 392-395)."""
    cb = MagicMock()
    events.subscribe_variable(1, "1", cb)
    fire_event(
        event_controller,
        _event(
            control="_1",
            action="6",
            event_info='<var type="1" id="1"><val>not-an-int</val></var>',
        ),
    )
    cb.assert_not_called()


def test_variable_event_with_unparseable_init_is_dropped(
    events, event_controller
) -> None:
    """Same for ``<init>`` on the init action (lines 400-401)."""
    cb = MagicMock()
    events.subscribe_variable(1, "1", cb)
    fire_event(
        event_controller,
        _event(
            control="_1",
            action="7",
            event_info='<var type="1" id="1"><init>not-an-int</init></var>',
        ),
    )
    cb.assert_not_called()


def test_variable_event_with_no_value_or_init_is_dropped(
    events, event_controller
) -> None:
    """An event whose action is value but the body has no ``<val>``
    yields ``value=None`` and is dropped (line 484)."""
    cb = MagicMock()
    events.subscribe_variable(1, "1", cb)
    fire_event(
        event_controller,
        _event(
            control="_1",
            action="6",
            event_info='<var type="1" id="1"></var>',
        ),
    )
    cb.assert_not_called()
