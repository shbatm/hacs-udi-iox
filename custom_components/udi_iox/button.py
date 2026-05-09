"""Representation of IoX buttons."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import (
    Controller,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    Node,
)
from pyisyox.constants import TAG_ENABLED, Protocol

from .const import CONF_NETWORK, DOMAIN
from .models import IsyConfigEntry, IsyData, NetworkResourceRecord


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IoX buttons from a config entry."""
    isy_data = config_entry.runtime_data
    controller: Controller = isy_data.root
    device_info = isy_data.devices
    entities: list[
        ISYNodeQueryButtonEntity
        | ISYNodeBeepButtonEntity
        | ISYNetworkResourceButtonEntity
    ] = []

    for node in isy_data.root_nodes[Platform.BUTTON]:
        entities.append(
            ISYNodeQueryButtonEntity(
                isy_data,
                node=node,
                name="Query",
                unique_id=f"{isy_data.uid_base(node)}_query",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_info=device_info[node.address],
            )
        )
        if node.protocol == Protocol.INSTEON:
            entities.append(
                ISYNodeBeepButtonEntity(
                    isy_data,
                    node=node,
                    name="Beep",
                    unique_id=f"{isy_data.uid_base(node)}_beep",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_info=device_info[node.address],
                )
            )

    for resource in isy_data.net_resources:
        entities.append(
            ISYNetworkResourceButtonEntity(
                isy_data,
                node=resource,
                name=resource.get("name", ""),
                unique_id=isy_data.uid_base(resource),
                device_info=device_info[CONF_NETWORK],
            )
        )

    # System-wide query button
    entities.append(
        ISYNodeQueryButtonEntity(
            isy_data,
            node=controller,
            name="Query",
            unique_id=f"{controller.config.uuid}_query",
            device_info=DeviceInfo(identifiers={(DOMAIN, controller.config.uuid)}),
            entity_category=EntityCategory.DIAGNOSTIC,
        )
    )

    async_add_entities(entities)


class ISYNodeButtonEntity(ButtonEntity):
    """Base for IoX device-button entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _node: Node | Controller | NetworkResourceRecord

    def __init__(
        self,
        isy_data: IsyData,
        node: Node | Controller | NetworkResourceRecord,
        name: str,
        unique_id: str,
        device_info: DeviceInfo,
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize a button entity."""
        self._isy_data = isy_data
        self._node = node

        self._attr_name = name
        self._attr_entity_category = entity_category
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        # NetworkResourceRecord (a dict) and Controller don't carry an
        # enabled flag; default to True so the button is always usable.
        self._node_enabled = getattr(node, TAG_ENABLED, True)
        self._unsubscribers: list[Callable[[], None]] = []

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return self._node_enabled

    async def async_added_to_hass(self) -> None:
        """Subscribe to lifecycle events for availability tracking."""
        if not isinstance(self._node, Node):
            # NetworkResource and system-query buttons aren't nodes.
            return
        self._unsubscribers.append(
            self._isy_data.controller_events.subscribe_lifecycle(self._on_lifecycle)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from controller events."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Update availability when the controller toggles the node."""
        if not isinstance(self._node, Node):
            return
        if event.node_address != self._node.address:
            return
        if event.action != NodeLifecycleAction.NODE_ENABLED:
            return
        self._node_enabled = getattr(self._node, TAG_ENABLED, True)
        self.async_write_ha_state()


class ISYNodeQueryButtonEntity(ISYNodeButtonEntity):
    """Press → :meth:`Node.query` (or :meth:`Controller.refresh`)."""

    _node: Node | Controller

    async def async_press(self) -> None:
        """Press the button."""
        if isinstance(self._node, Controller):
            await self._node.refresh()
        else:
            # Node.query is the v3 helper; pyisyox 6 routes it through
            # send_command(\"QUERY\") — same wire effect.
            await self._node.send_command("QUERY")


class ISYNodeBeepButtonEntity(ISYNodeButtonEntity):
    """Press → Insteon beep."""

    _node: Node

    async def async_press(self) -> None:
        """Press the button."""
        await self._node.send_command("BEEP")


class ISYNetworkResourceButtonEntity(ISYNodeButtonEntity):
    """Press → run an IoX network resource.

    Network resources aren't yet typed in pyisyox 6.0.0a1; the run
    surface is deferred to a later release. Pressing the button
    raises until that wrapper lands.
    """

    _attr_has_entity_name = False
    _node: NetworkResourceRecord

    async def async_press(self) -> None:
        """Press the button."""
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(
            "Network resource execution is not supported in this release"
        )
