"""IoX controller event handlers + per-entity dispatch registry.

Single source of subscriptions to the controller event / lifecycle
streams. Entities don't touch the controller directly: they call
:meth:`IsyControllerEvents.subscribe_node` /
:meth:`subscribe_lifecycle` / :meth:`subscribe_variable`, which add
their callback to a registry keyed by ``(node_address, control_id)``,
all-lifecycle, or ``(var_type, var_id)``.

The two top-level listeners we register on the controller fan out to
those registries — O(1) per event regardless of how many entities
share the same address. (Naïvely, every entity calling
``controller.add_event_listener`` directly would force the controller
to call N listeners per event, which on a real eisy with 200+ entities
is enough to matter.)
"""

from __future__ import annotations

from collections.abc import Callable
from xml.etree import ElementTree as ET

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.issue_registry as ir
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from pyisyox import (
    Controller,
    Event,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    ProgramStatusEvent,
    SystemEventControl,
)
from pyisyox.constants import EventStreamStatus

from .const import _LOGGER, DOMAIN
from .models import IsyData

ISSUE_LIFECYCLE_RELOAD = "lifecycle_reload_required"

#: Seconds to wait after a non-CONNECTED status before reporting entities
#: unavailable. The eisy emits a heartbeat every 30 s and pyisyox flags a
#: stream stale at ~60 s; brief reconnects within ~30 s of that window
#: are common (TLS-renegotiate, brief Wi-Fi hiccup, controller-side
#: reload). Reconnection during the debounce cancels the unavailable
#: flip entirely so the user never sees the flicker. Reconnect itself
#: is reported instantly — recovering availability fast is always fine.
WS_UNAVAILABLE_DEBOUNCE_SECONDS = 90

#: Per-property callback. Receives the raw ``Event`` from pyisyox.
NodeEventCallback = Callable[[Event], None]
#: Per-lifecycle callback. Receives the ``NodeLifecycleEvent``.
LifecycleCallback = Callable[[NodeLifecycleEvent], None]
#: Per-variable callback. Receives ``(value, init)`` extracted from the
#: ``<eventInfo><var ...>`` payload of a ``_1`` action=6/7 frame.
VariableEventCallback = Callable[[int | None, int | None], None]
#: Per-program callback. Receives the ``ProgramStatusEvent`` after
#: the matching ``ProgramRecord`` has been mutated in place.
ProgramEventCallback = Callable[[ProgramStatusEvent], None]
#: WS status callback. Receives the new connected flag (``True`` when the
#: event stream is up, ``False`` for every other status).
WsStatusCallback = Callable[[bool], None]

# Action codes carried inside a ``SystemEventControl.TRIGGER`` (``_1``)
# frame. pyisyox 6.0.0a2 keeps these private to the dispatcher — we
# duplicate them here only to disambiguate the variable / program
# branches of the system-event stream that the consumer also processes
# (`"6"` = variable value change, `"7"` = variable init change,
# `"0"` = program status).
_VAR_VALUE_ACTION = "6"
_VAR_INIT_ACTION = "7"


class IsyControllerEvents:
    """Wire pyisyox 6 controller events into HA + dispatch to entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        isy_data: IsyData,
        entry_id: str | None = None,
    ) -> None:
        """Subscribe to event + lifecycle streams from the controller.

        ``entry_id`` is required for the lifecycle Repair flow; when
        ``None`` the registry still routes events but skips creating
        repair issues (test fixtures that don't construct a config
        entry rely on this).
        """
        self.isy_data = isy_data
        self.hass = hass
        self.entry_id = entry_id
        self.dev_reg = dr.async_get(hass)
        controller: Controller = isy_data.root

        # Per-(address, control) registry. control == None matches every
        # control for that address (used when an entity wants every
        # update, e.g. binary sensors that re-evaluate on any change).
        self._node_listeners: dict[tuple[str, str | None], list[NodeEventCallback]] = {}
        # All-lifecycle listeners; consumers filter by address inside
        # the callback (cheap — lifecycle events are rare).
        self._lifecycle_listeners: list[LifecycleCallback] = []
        # Per-(var_type, var_id) registry. Variable change frames carry
        # the new value in <eventInfo>, which the dispatcher reads off
        # Event.event_info. When event_info isn't populated (older
        # pyisyox builds) variable dispatch silently no-ops and entities
        # fall back to the optimistic local update from their writes.
        self._variable_listeners: dict[
            tuple[str, str], list[VariableEventCallback]
        ] = {}
        # Per-program-id registry. pyisyox normalises the wire's
        # unpadded id ("8D") to the 4-character form ("008D") before
        # firing the listener, so consumers key on the 4-character id.
        self._program_listeners: dict[str, list[ProgramEventCallback]] = {}
        # WS-status listeners — fan out the {connected: bool} signal
        # entities use for the silver entity-unavailable rule.
        self._ws_status_listeners: list[WsStatusCallback] = []
        # Seed from the current WS status if the stream is already up
        # by the time we wire in; later status frames correct as needed.
        ws = controller.websocket
        self._ws_connected: bool = ws.connected if ws is not None else True
        # Un-debounced "stream is past the initial status replay" truth,
        # straight from pyisyox EventStreamStatus (CONNECTED only after
        # the post-connect replay drains; SYNCING/INITIALIZING/reconnect
        # => False). Distinct from ``_ws_connected``, which is debounced
        # to stop entities flapping unavailable on brief blips — that
        # debounce holds True across a fast reconnect, so it must NOT
        # gate event emission (the reconnect replay would leak through).
        # The ``event`` platform gates on this instead.
        self._stream_live: bool = ws.connected if ws is not None else True
        # Pending unavailable-flip timer. Set when the WS goes non-
        # CONNECTED so a brief blip-and-reconnect doesn't bounce every
        # entity through unavailable. Cleared on reconnect (within the
        # debounce window) or when it actually fires.
        self._ws_disconnect_timer: Callable[[], None] | None = None

        self._unsubscribe: list[Callable[[], None]] = [
            controller.add_event_listener(self._on_event),
            controller.add_node_lifecycle_listener(self._on_lifecycle),
            controller.add_program_status_listener(self._on_program_status),
        ]
        # ``Controller.add_status_listener`` errors out if the WS reader
        # isn't running yet. Tests that build a controller without
        # starting the WS (``start_websocket=False``) skip the
        # subscription — entities default to ``_ws_connected=True``
        # there, which matches the test fixture's "everything available"
        # expectation.
        if ws is not None:
            self._unsubscribe.append(controller.add_status_listener(self._on_ws_status))

    # --- subscription API for entities -----------------------------------

    @callback
    def subscribe_node(
        self,
        address: str,
        control: str | None,
        listener: NodeEventCallback,
    ) -> Callable[[], None]:
        """Register a callback for events on a single node.

        Args:
            address: Wire address of the node. Required.
            control: Property/control id (e.g. ``"ST"``, ``"OL"``).
                ``None`` matches every control on that address.
            listener: Callback invoked with the raw :class:`Event`.

        Returns:
            An unsubscribe callable. Idempotent.
        """
        key = (address, control)
        listeners = self._node_listeners.setdefault(key, [])
        listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                listeners.remove(listener)
            except ValueError:
                return
            if not listeners:
                # GC empty buckets so the dispatch dict doesn't grow
                # unboundedly across reloads.
                self._node_listeners.pop(key, None)

        return _unsubscribe

    @callback
    def subscribe_lifecycle(self, listener: LifecycleCallback) -> Callable[[], None]:
        """Register a callback for every NodeLifecycleEvent."""
        self._lifecycle_listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                self._lifecycle_listeners.remove(listener)
            except ValueError:
                return

        return _unsubscribe

    @callback
    def subscribe_variable(
        self,
        var_type: int | str,
        var_id: int | str,
        listener: VariableEventCallback,
    ) -> Callable[[], None]:
        """Register a callback for a controller variable's change frames.

        Variable change events flow through the ``_1`` system control
        with action ``"6"`` (current value) or ``"7"`` (init value);
        the ``<var type="..." id="..."><val>`` payload disambiguates
        which variable.

        Args:
            var_type: ``1`` (integer) or ``2`` (state).
            var_id: Numeric variable id.
            listener: Callback invoked with ``(value, init)``. Exactly
                one of the two will be non-``None`` per call.

        Returns:
            An unsubscribe callable.
        """
        key = (str(var_type), str(var_id))
        listeners = self._variable_listeners.setdefault(key, [])
        listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                listeners.remove(listener)
            except ValueError:
                return
            if not listeners:
                self._variable_listeners.pop(key, None)

        return _unsubscribe

    @callback
    def subscribe_program(
        self, program_id: str, listener: ProgramEventCallback
    ) -> Callable[[], None]:
        """Register a callback for one program's status frames.

        pyisyox normalises the wire's unpadded program id (``"8D"``)
        to the 4-character ``/api/programs`` form (``"008D"``) before
        firing the upstream listener, so subscribe with the same
        4-character id you read from ``controller.programs[id]``.

        ``ProgramRecord.status`` / ``running`` are mutated in place
        before this callback fires, so reading
        ``program.status`` from inside the callback returns the new
        value.

        Returns:
            An unsubscribe callable. Idempotent.
        """
        listeners = self._program_listeners.setdefault(program_id, [])
        listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                listeners.remove(listener)
            except ValueError:
                return
            if not listeners:
                self._program_listeners.pop(program_id, None)

        return _unsubscribe

    @callback
    def subscribe_ws_status(self, listener: WsStatusCallback) -> Callable[[], None]:
        """Register a callback for WS connected/disconnected flips.

        Fires only on a true edge — repeat status frames carrying the
        same connected value are deduped. Listener is invoked with the
        new ``connected: bool``; entities just call
        :meth:`Entity.async_write_ha_state` so HA re-reads ``available``.
        """
        self._ws_status_listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                self._ws_status_listeners.remove(listener)
            except ValueError:
                return

        return _unsubscribe

    @property
    def ws_connected(self) -> bool:
        """Whether the event stream is currently up. Defaults ``True``
        when there's no WS (test fixtures opt out of the upgrade)."""
        return self._ws_connected

    @property
    def stream_live(self) -> bool:
        """Whether the stream is past the initial status replay (raw
        ``EventStreamStatus.CONNECTED``, un-debounced). The ``event``
        platform gates emission on this so the controller's
        replay-on-connect (every reconnect/restart/reload) doesn't fire
        spurious events. Defaults ``True`` when there's no WS (test
        fixtures opt out of the upgrade)."""
        return self._stream_live

    @callback
    def stop(self) -> None:
        """Drop all controller subscriptions."""
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        self._node_listeners.clear()
        self._lifecycle_listeners.clear()
        self._variable_listeners.clear()
        self._program_listeners.clear()
        self._ws_status_listeners.clear()
        if self._ws_disconnect_timer is not None:
            self._ws_disconnect_timer()
            self._ws_disconnect_timer = None

    # --- pyisyox listener entry points -----------------------------------

    @callback
    def _on_event(self, event: Event) -> None:
        """Dispatch a property event to entity listeners.

        Order:
        1. Invoke every listener registered for ``(address, control)``.
        2. Invoke every wildcard listener registered for
           ``(address, None)``.
        """
        if not event.node_address:
            # System events. Variable + program changes ride here on the
            # ``SystemEventControl.TRIGGER`` frame with the payload in
            # event_info — that's the only system event this integration
            # acts on. Everything else (heartbeats, config toggles,
            # lifecycle verbs, Z-Wave/Matter status, ...) is logged by
            # pyisyox itself at DEBUG; duplicating it here would just
            # double every line in `verbose:` traces.
            if event.control == SystemEventControl.TRIGGER and event.action in (
                _VAR_VALUE_ACTION,
                _VAR_INIT_ACTION,
            ):
                self._dispatch_variable_event(event)
            return

        address = event.node_address

        # Per-control listeners
        for listener in tuple(self._node_listeners.get((address, event.control), ())):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "IoX listener for %s/%s raised", address, event.control
                )
        # Wildcard ("any control") listeners
        for listener in tuple(self._node_listeners.get((address, None), ())):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("IoX wildcard listener for %s raised", address)

    @callback
    def _dispatch_variable_event(self, event: Event) -> None:
        """Parse the ``<var type id><val>/<init>`` payload and fan out."""
        event_info = getattr(event, "event_info", "") or ""
        if not event_info:
            return

        try:
            # event_info is the inner XML (no <eventInfo> wrapper) — wrap
            # it so ElementTree has a single root element to parse.
            root = ET.fromstring(f"<wrap>{event_info}</wrap>")  # noqa: S314
        except ET.ParseError:
            _LOGGER.debug(
                "IoX variable event_info unparsable; dropping (control=%s action=%s)",
                event.control,
                event.action,
            )
            return

        var_el = root.find("var")
        if var_el is None:
            return
        var_type = var_el.get("type", "")
        var_id = var_el.get("id", "")
        if not var_type or not var_id:
            return

        value: int | None = None
        init: int | None = None
        if event.action == _VAR_VALUE_ACTION:
            value_el = var_el.find("val")
            if value_el is not None and value_el.text:
                try:
                    value = int(value_el.text)
                except ValueError:
                    return
        else:  # init action
            init_el = var_el.find("init")
            if init_el is not None and init_el.text:
                try:
                    init = int(init_el.text)
                except ValueError:
                    return
        if value is None and init is None:
            return

        for listener in tuple(self._variable_listeners.get((var_type, var_id), ())):
            try:
                listener(value, init)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "IoX variable listener for %s/%s raised", var_type, var_id
                )

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Dispatch a node-lifecycle event to all subscribers.

        pyisyox already logs the raw lifecycle frame at DEBUG
        (``System event: node_lifecycle = …``); here we only act on it.
        ``NodeLifecycleAction.label`` lower-cases the member name (or
        echoes the raw value for codes pyisyox doesn't know); we
        title-case it for the Repair card.
        """
        verb = NodeLifecycleAction.label(event.raw_action)

        # The reload-required verbs invalidate the cached node registry
        # (added/removed/renamed/enabled/revised/removed-from-scene). HA
        # has no live-merge path for those, so surface a Repair card
        # that lets the user trigger a reload at a safe moment.
        if event.requires_reload and self.entry_id is not None:
            self._raise_reload_repair(event, verb)

        for listener in tuple(self._lifecycle_listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("IoX lifecycle listener raised")

    @callback
    def _on_program_status(self, event: ProgramStatusEvent) -> None:
        """Dispatch a program-status update to entities subscribed to that id.

        pyisyox already mutated ``ProgramRecord.status`` /
        ``running`` before firing this; entities just need to refresh
        their HA state.
        """
        listeners = self._program_listeners.get(event.address, ())
        for listener in tuple(listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("IoX program listener for %s raised", event.address)

    @callback
    def _on_ws_status(self, status: EventStreamStatus) -> None:
        """Boil ``EventStreamStatus`` down to ``connected: bool``.

        Non-CONNECTED transitions are debounced by
        :data:`WS_UNAVAILABLE_DEBOUNCE_SECONDS` (eisy blips and
        reconnects within seconds). Reconnect reported instantly;
        WARN on the unavailable flip, INFO on reconnect.
        """
        actually_connected = status == EventStreamStatus.CONNECTED
        # Track the live/replay state immediately (no debounce) so the
        # event platform never emits the post-connect status replay,
        # incl. on a fast reconnect where ``_ws_connected`` stays True.
        self._stream_live = actually_connected

        if actually_connected:
            # Reconnect: cancel any pending unavailable flip; if
            # entities were already flipped to unavailable, flip them
            # back now.
            if self._ws_disconnect_timer is not None:
                self._ws_disconnect_timer()
                self._ws_disconnect_timer = None
            if self._ws_connected:
                return
            self._ws_connected = True
            _LOGGER.info(
                "IoX event stream reconnected (%s)",
                self.isy_data.root.config.uuid,
            )
            self._fan_out_ws_status(True)
            return

        # Non-CONNECTED frame. We're already disconnected: nothing
        # changes for entities. (The status enum may flip among the
        # various non-CONNECTED values during a slow reconnect; we
        # don't act on those.)
        if not self._ws_connected:
            return
        # Already scheduled a flip; don't reschedule.
        if self._ws_disconnect_timer is not None:
            return
        self._ws_disconnect_timer = async_call_later(
            self.hass,
            WS_UNAVAILABLE_DEBOUNCE_SECONDS,
            self._fire_ws_unavailable,
        )
        _LOGGER.debug(
            "IoX event stream non-CONNECTED (status=%s); marking "
            "entities unavailable in %ds unless it recovers",
            status.value,
            WS_UNAVAILABLE_DEBOUNCE_SECONDS,
        )

    @callback
    def _fire_ws_unavailable(self, _now: object) -> None:
        """Debounce timer fired without a reconnect — flip unavailable."""
        self._ws_disconnect_timer = None
        self._ws_connected = False
        _LOGGER.warning(
            "IoX event stream did not reconnect within %ds — entities "
            "for controller %s now report unavailable",
            WS_UNAVAILABLE_DEBOUNCE_SECONDS,
            self.isy_data.root.config.uuid,
        )
        self._fan_out_ws_status(False)

    @callback
    def _fan_out_ws_status(self, connected: bool) -> None:
        """Invoke every subscribed WS-status listener."""
        for listener in tuple(self._ws_status_listeners):
            try:
                listener(connected)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("IoX ws-status listener raised")

    @callback
    def _raise_reload_repair(self, event: NodeLifecycleEvent, verb: str) -> None:
        """Create or refresh the lifecycle-reload Repair issue.

        Coalesces by entry_id: subsequent reload-required events while
        the card is up overwrite the placeholders (most-recent verb +
        address) rather than spawning duplicates. The card stays open
        until the user submits the fix flow, which reloads the entry
        and clears the issue.
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_LIFECYCLE_RELOAD}.{self.entry_id}",
            data={"entry_id": self.entry_id},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LIFECYCLE_RELOAD,
            translation_placeholders={
                "verb": verb.replace("_", " ").title(),
                "address": event.node_address or "(controller)",
            },
        )
