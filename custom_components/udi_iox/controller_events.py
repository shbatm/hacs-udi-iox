"""IoX controller event handlers + per-entity dispatch registry.

Single source of subscriptions to the pyisyox 6 controller event /
lifecycle streams. Entities don't touch the controller directly:
they call :meth:`IsyControllerEvents.subscribe_node` /
:meth:`subscribe_lifecycle`, which add their callback to a registry
keyed by ``(node_address, control_id_or_None)``.

The two top-level listeners we register on the controller fan out
to those registries — O(1) per event regardless of how many
entities share the same address. (Naïvely, every entity calling
``controller.add_event_listener`` directly would force the controller
to call N listeners per event, which on a real eisy with 200+ entities
is enough to matter.)

Lifecycle handling currently logs each verb. Per fork plan §Phase 5
the next step is to wire HA Repair cards keyed off
:class:`NodeLifecycleAction`:

* ``ND`` (added)            → "New IoX device detected — reload?"
* ``NN`` (renamed)          → entity-registry name update, no reload
* ``NR`` (removed)          → entity unavailable + Repair "delete?"
* ``MV`` / ``RG`` / ``PC``  → re-evaluate device area / parent

That dispatch is the next follow-up after entity subscriptions stabilize.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.core import HomeAssistant, callback
from pyisyox import (
    Controller,
    Event,
    NodeLifecycleAction,
    NodeLifecycleEvent,
)

from .const import _LOGGER, DOMAIN, EVENT_UDI_IOX_CONTROL
from .models import IsyData

#: Per-property callback. Receives the raw ``Event`` from pyisyox.
NodeEventCallback = Callable[[Event], None]
#: Per-lifecycle callback. Receives the ``NodeLifecycleEvent``.
LifecycleCallback = Callable[[NodeLifecycleEvent], None]


class IsyControllerEvents:
    """Wire pyisyox 6 controller events into HA + dispatch to entities."""

    def __init__(self, hass: HomeAssistant, isy_data: IsyData) -> None:
        """Subscribe to event + lifecycle streams from the controller."""
        self.isy_data = isy_data
        self.hass = hass
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
    def stop(self) -> None:
        """Drop all controller subscriptions."""
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        self._node_listeners.clear()
        self._lifecycle_listeners.clear()

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
            # System events (heartbeat, status). Nothing addressable
            # to dispatch — log and move on.
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

        for listener in tuple(self._lifecycle_listeners):
            try:
                listener(event)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("IoX lifecycle listener raised")
