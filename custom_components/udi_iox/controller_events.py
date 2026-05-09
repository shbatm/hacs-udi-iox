"""The ISY/IoX integration event handler class models."""

from __future__ import annotations

from dataclasses import asdict

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.core import HomeAssistant, callback
from pyisyox import (
    Controller,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    NodePropertyValue,
)
from pyisyox.constants import SystemStatus

from .const import _LOGGER, DOMAIN, EVENT_UDI_IOX_CONTROL
from .models import IsyData


class IsyControllerEvents:
    """Represent ISY/IoX controller events."""

    def __init__(self, hass: HomeAssistant, isy_data: IsyData) -> None:
        """Set up the driver events instance."""
        self.isy_data = isy_data
        self.hass = hass
        self.dev_reg = dr.async_get(hass)
        self.entity_reg = er.async_get(hass)
        isy: Controller = self.isy_data.root
        self.listeners = [
            isy.nodes.status_events.subscribe(self.node_event_handler),
            isy.nodes.platform_events.subscribe(self.node_change_handler),
            isy.programs.status_events.subscribe(self.program_event_handler),
            isy.status_events.subscribe(self.system_status_handler),
        ]

    @callback
    def node_event_handler(self, event: NodePropertyValue) -> None:
        """Handle node control event sent from ISY."""
        unique_id = self.isy_data.uid_base(event)
        entity_id = None

        # Try and find the entity_id if an entity exists
        if platform := self.isy_data.node_event_unique_ids.get(unique_id):
            entity_id = self.entity_reg.async_get_entity_id(
                platform,
                DOMAIN,
                unique_id,
            )
        control_event = {"entity_id": entity_id, **asdict(event)}
        self.hass.bus.async_fire(EVENT_UDI_IOX_CONTROL, control_event)

    @callback
    def node_change_handler(self, event: NodeLifecycleEvent) -> None:
        """Handle a node lifecycle event from the controller."""
        _LOGGER.debug(
            "IoX updated configuration: Address %s Changed: %s."
            " Integration should be reloaded to pick up new changes",
            event.address,
            NodeLifecycleAction(event.action).name.replace("_", " ").title(),
        )
        # Future: this is a logging call for now. Future PR to add support for
        # adding and removing nodes on the fly.

    @callback
    def program_event_handler(self, event: dict) -> None:
        """Handle program control event sent from the controller."""
        self.hass.bus.async_fire("udi_iox_program_event", event)

    @callback
    def system_status_handler(self, event: SystemStatus) -> None:
        """Handle a system status changed event sent ISY class."""
        _LOGGER.debug("System status changed: %s", event.name.replace("_", " ").title())
        # Future: this is a logging call for now. Future PR to add support for
        # a entity representing the hub's current status (busy, safe-mode, idle).
