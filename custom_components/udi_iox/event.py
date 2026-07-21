"""Event entities for IoX nodes that emit control verbs.

One entity per node whose nodedef declares a non-empty ``cmds.sends``
list; ``event_types`` derive from that list (slugified name → wire id).
Sub-button entities (KeypadLinc accessory buttons) are
disabled-by-default — a single keypad would otherwise register 6-8
unused entities.

A KeypadLinc/RemoteLinc sub-button's last-commanded level rides along
as the ``button_status`` extra-state-attribute (#85, amended per
#101) -- see ``ISYButtonEvent.extra_state_attributes`` for scoping and
staleness rules.
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

from .entity import ISYNodeEntity, _resolve_device_info, node_status_int

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .models import IsyConfigEntry, IsyData

# EventEntity unique-id suffix; re-imported by models.py for stale-entity
# cleanup so both sides stay in sync.
EVENT_BUTTON_UNIQUE_ID_SUFFIX = "_button"

# Verbs that mark a node as "button-shaped" → BUTTON device class.
# Motion/doorbell plugins (just DOORBELL_PRESS) get no device class.
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
        # KeypadLinc sub-buttons stay disabled by default; users opt in.
        if node.primary_address is not None:
            self._attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self) -> None:
        """Wildcard-subscribe to every control on this node — the event
        platform needs every verb, not just one."""
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

    @property
    def extra_state_attributes(self) -> dict:
        """Last-commanded level for KeypadLinc/RemoteLinc sub-buttons
        (#85, option (b), amended per #101).

        Scoped to sub-button nodes only (``primary_address is not
        None``) -- a standalone node's own switch/light/sensor entity
        already surfaces its ``ST``, so this attribute only fills a
        real gap for KeypadLinc/RemoteLinc sub-buttons, which have no
        other entity of their own. Kept as ``Node.status``'s reported
        level, **not** coerced to a bool like
        ``ISYBinarySensorEntity.is_on`` — a button wired to a fade
        up/down load reports its current dim level here (already
        UOM-normalized by pyisyox, same as every other dimmable
        entity), not just on/off. Omitted entirely for nodes that
        don't report ``ST``, including the controller's own "unknown"
        marker, rather than showing a misleading ``None`` -- or worse,
        a fabricated int coerced from that marker.

        Only refreshed on a real press (see ``_on_control``) -- no
        independent ``ST``-driven update exists, so this can go stale
        relative to the physical device (e.g. the linked load changing
        state via another automation). Trust the button's actual LED
        state only via scene membership, not this attribute.
        """
        if self._node.primary_address is None:
            return {}
        status = node_status_int(self._node)
        return {} if status is None else {"button_status": status}

    @callback
    def _on_control(self, event: Event) -> None:
        """Fire the matching event_type when one of the node's verbs arrives.

        The wildcard subscription delivers *every* control on this node
        (status reports, etc.); only the ones declared in the nodedef's
        ``cmds.sends`` map to an event_type — the rest, including bare
        ``ST`` status reports, are ignored entirely. (#101: a prior
        version wrote ha state on every ``ST`` report to refresh
        ``button_status`` independently of a press, but that write
        fires ``state_changed`` regardless of ``stream_live``, so any
        out-of-band ``ST`` change produced a bare state-trigger fire
        with a stale ``event_type`` attribute.)

        The controller replays every node's *current* status on every
        WebSocket (re)connect (HA restart / config-entry reload / eisy
        blip). pyisyox holds ``EventStreamStatus.SYNCING`` until that
        replay drains; until then ``stream_live`` is False and we drop
        the frame for *event firing* — replayed status is not a live
        button press, and emitting it fires spurious automations on
        every connect.
        """
        if not self._isy_data.controller_events.stream_live:
            return
        event_type = self._event_type_by_control.get(event.control)
        if event_type is None:
            return
        attributes = {"event_info": event.event_info} if event.event_info else None
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
