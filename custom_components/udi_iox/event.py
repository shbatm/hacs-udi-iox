"""Event entities for IoX Insteon load and keypad-button nodes.

Each entity represents a physical button on the device and emits one of the
``event_types`` below when its corresponding control event arrives from the
controller. KeypadLinc sub-button entities are disabled by default to avoid
registering large numbers of unused entities for users who don't need them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import Event, Node, NodeLifecycleAction, NodeLifecycleEvent
from pyisyox.constants import (
    CMD_FADE_DOWN,
    CMD_FADE_STOP,
    CMD_FADE_UP,
    CMD_OFF,
    CMD_OFF_FAST,
    CMD_ON,
    CMD_ON_FAST,
)

from .entity import ISYNodeEntity, _resolve_device_info

if TYPE_CHECKING:
    from .models import IsyConfigEntry, IsyData

# Suffix for the unique-id of the EventEntity each button-emitting node
# spawns. Imported back into models.py (which derives unique-ids during
# stale-entity cleanup); keep this constant next to the EventEntity that
# owns the format so both sides stay in sync.
EVENT_BUTTON_UNIQUE_ID_SUFFIX = "_button"

CONTROL_TO_EVENT_TYPE: Final[dict[str, str]] = {
    CMD_ON: "on",
    CMD_OFF: "off",
    CMD_ON_FAST: "fast_on",
    CMD_OFF_FAST: "fast_off",
    CMD_FADE_UP: "fade_up",
    CMD_FADE_DOWN: "fade_down",
    CMD_FADE_STOP: "fade_stop",
}

BUTTON_DESCRIPTION: Final[EventEntityDescription] = EventEntityDescription(
    key="button",
    translation_key="button",
    device_class=EventDeviceClass.BUTTON,
    event_types=list(CONTROL_TO_EVENT_TYPE.values()),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the IoX event platform."""
    isy_data = entry.runtime_data
    device_info = isy_data.devices
    async_add_entities(
        ISYButtonEvent(isy_data, node, _resolve_device_info(device_info, node))
        for node in isy_data.nodes[Platform.EVENT]
    )


class ISYButtonEvent(ISYNodeEntity, EventEntity):
    """Event entity that emits press/fast/fade events from a node."""

    entity_description = BUTTON_DESCRIPTION
    _attr_has_entity_name = True

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the IoX button event entity."""
        super().__init__(isy_data, node, device_info=device_info)
        self._attr_unique_id = (
            f"{isy_data.uuid}_{node.address}{EVENT_BUTTON_UNIQUE_ID_SUFFIX}"
        )
        # ISYNodeEntity already computes ``_attr_name``: ``None`` for
        # device-root nodes, stripped sub-name for sub-buttons. Just
        # disable-by-default the sub-button case so a KeypadLinc's 6–8
        # accessory buttons don't clutter the entity registry until
        # users explicitly opt them in.
        if node.primary_address is not None:
            self._attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to every control event for this node + lifecycle.

        Unlike the ISYNodeEntity default which filters to one control
        id, the event platform needs every fade/fast/press code, so
        the (address, None) wildcard subscription is correct here.
        """
        events = self._isy_data.controller_events
        self._unsubscribers.append(
            events.subscribe_node(self._node.address, None, self._on_control)
        )
        self._unsubscribers.append(events.subscribe_lifecycle(self._on_lifecycle))

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Refresh availability when the controller toggles the node."""
        if event.node_address != self._node.address:
            return
        if event.action != NodeLifecycleAction.NODE_ENABLED:
            return
        self._attr_available = self._node.enabled
        self.async_write_ha_state()

    @callback
    def _on_control(self, event: Event) -> None:
        """Fire the matching event_type when a known control arrives."""
        event_type = CONTROL_TO_EVENT_TYPE.get(event.control)
        if event_type is None:
            return
        self._trigger_event(event_type)
        self.async_write_ha_state()
