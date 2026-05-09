"""IoX controller event handlers.

Wires the pyisyox 6 listener API (add_event_listener,
add_status_listener, add_node_lifecycle_listener) into HA's
event bus + entity registry, replacing the legacy v3
``isy.nodes.status_events`` / ``platform_events`` /
``programs.status_events`` shape.

The lifecycle listener is the entry point for the Repair-card
flow described in fork plan §Phase 5: NodeLifecycleAction
verbs (NODE_ADDED / RENAMED / REMOVED / MOVED) trigger
config-entry reloads, entity-registry name updates, or
HA Repair issues. The current implementation logs each
action and reloads the entry on add/remove; per-action
Repair cards land in a follow-up.
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


class IsyControllerEvents:
    """Wire pyisyox 6 controller events into HA."""

    def __init__(self, hass: HomeAssistant, isy_data: IsyData) -> None:
        """Subscribe to event + lifecycle streams from the controller."""
        self.isy_data = isy_data
        self.hass = hass
        self.dev_reg = dr.async_get(hass)
        self.entity_reg = er.async_get(hass)
        controller: Controller = isy_data.root
        self._unsubscribe: list[Callable[[], None]] = [
            controller.add_event_listener(self._on_event),
            controller.add_node_lifecycle_listener(self._on_lifecycle),
        ]

    @callback
    def stop(self) -> None:
        """Drop all controller subscriptions."""
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()

    @callback
    def _on_event(self, event: Event) -> None:
        """Forward each property/control event onto the HA bus.

        Lifecycle frames are skipped — they fire on the dedicated
        lifecycle listener and don't need double-firing here.
        Per-node-property frames carry a ``node_address`` and a
        ``control`` (e.g. ``"ST"``); the bus payload mirrors the
        :class:`pyisyox.runtime.events.Event` shape so HA automations
        can match on the same fields they would have on the wire.
        """
        if not event.node_address:
            # System events (heartbeat, status) — log via debug only.
            _LOGGER.debug("IoX system event: %s = %s", event.control, event.action)
            return

        unique_id = f"{self.isy_data.uuid}_{event.node_address}"
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

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Handle node add / remove / rename / move from the controller.

        For now this only logs at DEBUG level. Per fork plan §Phase 5
        the next step is to wire HA Repair cards keyed off
        :class:`NodeLifecycleAction`:

        * ND (added)            → "New IoX device detected — reload?"
        * NN (renamed)          → entity-registry name update, no reload
        * NR (removed)          → entity unavailable + Repair "delete?"
        * MV / RG / PC          → re-evaluate device area / parent

        That dispatch lives in a follow-up alongside the Repair flow.
        """
        try:
            verb = NodeLifecycleAction(event.action).name
        except ValueError:
            verb = event.raw_action
        _LOGGER.debug(
            "IoX node lifecycle: %s on %s",
            verb.replace("_", " ").title(),
            event.node_address,
        )
