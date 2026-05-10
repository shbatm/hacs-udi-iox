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
from dataclasses import asdict, is_dataclass
from xml.etree import ElementTree as ET

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
import homeassistant.helpers.issue_registry as ir
from homeassistant.core import HomeAssistant, callback
from pyisyox import (
    Controller,
    Event,
    NodeLifecycleAction,
    NodeLifecycleEvent,
)

from .const import _LOGGER, DOMAIN, EVENT_UDI_IOX_CONTROL
from .models import IsyData

ISSUE_LIFECYCLE_RELOAD = "lifecycle_reload_required"

#: Per-property callback. Receives the raw ``Event`` from pyisyox.
NodeEventCallback = Callable[[Event], None]
#: Per-lifecycle callback. Receives the ``NodeLifecycleEvent``.
LifecycleCallback = Callable[[NodeLifecycleEvent], None]
#: Per-variable callback. Receives ``(value, init)`` extracted from the
#: ``<eventInfo><var ...>`` payload of a ``_1`` action=6/7 frame.
VariableEventCallback = Callable[[int | None, int | None], None]

# IoX system control codes for variables and programs. The action code
# disambiguates: ``"6"`` = current value change, ``"7"`` = init change,
# ``"0"``/``"3"`` = program-related.
_VAR_OR_PROG_CONTROL = "_1"
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
        self.entity_reg = er.async_get(hass)
        controller: Controller = isy_data.root

        # Per-(address, control) registry. control == None matches every
        # control for that address (used when an entity wants every
        # update, e.g. binary sensors that re-evaluate on any change).
        self._node_listeners: dict[
            tuple[str, str | None], list[NodeEventCallback]
        ] = {}
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

        self._unsubscribe: list[Callable[[], None]] = [
            controller.add_event_listener(self._on_event),
            controller.add_node_lifecycle_listener(self._on_lifecycle),
        ]

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
    def subscribe_lifecycle(
        self, listener: LifecycleCallback
    ) -> Callable[[], None]:
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
    def stop(self) -> None:
        """Drop all controller subscriptions."""
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        self._node_listeners.clear()
        self._lifecycle_listeners.clear()
        self._variable_listeners.clear()

    # --- pyisyox listener entry points -----------------------------------

    @callback
    def _on_event(self, event: Event) -> None:
        """Dispatch a property event to entity listeners + the HA bus.

        Order:
        1. Fire ``udi_iox_control`` on the HA bus (matches the legacy
           ``isy994_control`` surface so existing automations keep
           working).
        2. Invoke every listener registered for ``(address, control)``.
        3. Invoke every wildcard listener registered for
           ``(address, None)``.
        """
        if not event.node_address:
            # System events. Variable + program changes ride here on
            # control "_1" with the payload in event_info; everything
            # else just gets a debug log.
            if event.control == _VAR_OR_PROG_CONTROL and event.action in (
                _VAR_VALUE_ACTION,
                _VAR_INIT_ACTION,
            ):
                self._dispatch_variable_event(event)
                return
            _LOGGER.debug("IoX system event: %s = %s", event.control, event.action)
            return

        address = event.node_address
        unique_id = f"{self.isy_data.uuid}_{address}"
        platform = self.isy_data.node_event_unique_ids.get(unique_id)
        entity_id = (
            self.entity_reg.async_get_entity_id(platform, DOMAIN, unique_id)
            if platform
            else None
        )
        payload = asdict(event) if is_dataclass(event) else {"event": repr(event)}
        self.hass.bus.async_fire(
            EVENT_UDI_IOX_CONTROL, {"entity_id": entity_id, **payload}
        )

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
                _LOGGER.exception(
                    "IoX wildcard listener for %s raised", address
                )

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
                "IoX variable event_info unparseable; dropping (control=%s action=%s)",
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

        for listener in tuple(
            self._variable_listeners.get((var_type, var_id), ())
        ):
            try:
                listener(value, init)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "IoX variable listener for %s/%s raised", var_type, var_id
                )

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Dispatch a node-lifecycle event to all subscribers."""
        try:
            verb = NodeLifecycleAction(event.action).name
        except ValueError:
            verb = event.raw_action
        _LOGGER.debug(
            "IoX node lifecycle: %s on %s",
            verb.replace("_", " ").title(),
            event.node_address,
        )

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
    def _raise_reload_repair(
        self, event: NodeLifecycleEvent, verb: str
    ) -> None:
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
