"""Event entities for IoX nodes that emit control verbs.

Each node whose nodedef declares a non-empty ``cmds.sends`` list gets one
event entity. The entity's ``event_types`` are derived from that list:
the human-readable command name slugified (``"Fast On"`` → ``fast_on``),
falling back to the lowercased wire id when a command carries no name.
That covers both native Insteon load/keypad nodes (``DON`` / ``DOF`` /
fast / fade / brighten / dim) and PG3 plugin trigger sources (which
publish their own verbs, e.g. ``DOORBELL_PRESS`` → ``doorbell_press``).

Translations for the common Insteon verbs live in ``strings.json`` under
``entity.event.button``; verbs without a translation render as the raw
slug.

Sub-button entities (KeypadLinc accessory buttons) are disabled by
default to avoid registering large numbers of unused entities for users
who don't need them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify
from pyisyox import Event, Node, NodeLifecycleAction, NodeLifecycleEvent
from pyisyox.constants import CMD_OFF, CMD_ON
from pyisyox.schema.nodedef import Command

from .entity import ISYNodeEntity, _resolve_device_info

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .models import IsyConfigEntry, IsyData

# Suffix for the unique-id of the EventEntity each emitting node spawns.
# Imported back into models.py (which derives unique-ids during
# stale-entity cleanup); keep this constant next to the EventEntity that
# owns the format so both sides stay in sync.
EVENT_BUTTON_UNIQUE_ID_SUFFIX = "_button"

# Wire command ids that mark a node as "button-shaped" — only those get
# the BUTTON device class. A motion/doorbell plugin that merely sends
# DOORBELL_PRESS isn't a button, so it gets no device class.
_BUTTON_SHAPED_VERBS = frozenset({CMD_ON, CMD_OFF})


def event_type_for_command(command: Command) -> str:
    """Slug for a sent command — its name if it has one, else the wire id."""
    return slugify(command.name) or command.id.lower()


PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the IoX event platform."""
    isy_data = entry.runtime_data
    device_info = isy_data.devices
    async_add_entities(
        ISYButtonEvent(
            isy_data,
            node,
            isy_data.node_triggers[node.address],
            _resolve_device_info(device_info, node),
        )
        for node in isy_data.nodes[Platform.EVENT]
    )


class ISYButtonEvent(ISYNodeEntity, EventEntity):
    """Event entity that emits a node's sent control verbs."""

    _attr_has_entity_name = True
    _attr_translation_key = "button"

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        triggers: list[Command],
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the IoX button event entity."""
        super().__init__(isy_data, node, device_info=device_info)
        self._attr_unique_id = (
            f"{isy_data.uuid}_{node.address}{EVENT_BUTTON_UNIQUE_ID_SUFFIX}"
        )
        # Wire id -> event_type, used by ``_on_control``. Building the
        # dict first dedupes any commands that slug to the same value
        # while preserving first-seen order for ``event_types``.
        self._event_type_by_control: dict[str, str] = {
            cmd.id: event_type_for_command(cmd) for cmd in triggers
        }
        self._attr_event_types = list(
            dict.fromkeys(self._event_type_by_control.values())
        )
        if _BUTTON_SHAPED_VERBS.intersection(self._event_type_by_control):
            self._attr_device_class = EventDeviceClass.BUTTON
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
        id, the event platform needs every verb the node emits, so the
        (address, None) wildcard subscription is correct here.
        """
        events = self._isy_data.controller_events
        self._unsubscribers.append(
            events.subscribe_node(self._node.address, None, self._on_control)
        )
        self._unsubscribers.append(events.subscribe_lifecycle(self._on_lifecycle))
        self._unsubscribers.append(events.subscribe_ws_status(self._on_ws_status))

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
        """Fire the matching event_type when one of the node's verbs arrives.

        The wildcard subscription delivers *every* control on this node
        (status reports, etc.); only the ones declared in the nodedef's
        ``cmds.sends`` map to an event_type — the rest are ignored.
        """
        event_type = self._event_type_by_control.get(event.control)
        if event_type is None:
            return
        attributes = {"event_info": event.event_info} if event.event_info else None
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
