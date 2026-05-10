"""Support for ISY switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import Group, Node, NodeCommandError, Program
from pyisyox.constants import CMD_OFF, CMD_ON

from .entity import ISYGroupEntity, ISYNodeEntity, ISYProgramEntity, NodeEventType, node_status_int
from .models import IsyConfigEntry, IsyData


@dataclass
class ISYSwitchEntityDescription(SwitchEntityDescription):
    """Describes IST switch."""

    # ISYEnableSwitchEntity does not support UNDEFINED or None,
    # restrict the type to str.
    name: str = ""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ISY switch platform."""
    isy_data = entry.runtime_data
    entities: list[
        ISYSwitchEntity
        | ISYGroupSwitchEntity
        | ISYSwitchProgramEntity
        | ISYEnableSwitchEntity
    ] = []
    device_info = isy_data.devices
    for node in isy_data.nodes[Platform.SWITCH]:
        entities.append(
            ISYSwitchEntity(
                isy_data,
                node=node,
                device_info=device_info.get(node.primary_node),
            )
        )

    for group in isy_data.groups:
        device = None
        if group.controller_addresses and len(group.controller_addresses) == 1:
            # If Group has only one controller, link to that device
            # instead of the hub.
            primary_addr = group.controller_addresses[0]
            controller_node = isy_data.root.nodes.get(primary_addr)
            if controller_node is not None:
                device = device_info.get(controller_node.primary_node)
        entities.append(ISYGroupSwitchEntity(isy_data, node=group, device_info=device))

    for name, status, actions in isy_data.programs[Platform.SWITCH]:
        entities.append(ISYSwitchProgramEntity(isy_data, name, status, actions))

    for node, control in isy_data.aux_properties[Platform.SWITCH]:
        # Currently only used for enable switches, will need to be updated for
        # NS support by making sure control == TAG_ENABLED
        description = ISYSwitchEntityDescription(
            key=control,
            device_class=SwitchDeviceClass.SWITCH,
            name=control.title(),
            entity_category=EntityCategory.CONFIG,
        )
        entities.append(
            ISYEnableSwitchEntity(
                isy_data,
                node=node,
                control=control,
                unique_id=f"{isy_data.uid_base(node)}_{control}",
                description=description,
                device_info=device_info.get(node.primary_node),
            )
        )
    async_add_entities(entities)


class ISYSwitchEntityMixin(SwitchEntity):
    """Representation of an ISY switch device."""

    _node: Node | Group

    @property
    def is_on(self) -> bool | None:
        """Get whether the ISY device is in the on state."""
        if node_status_int(self._node) is None:
            return None
        return bool(node_status_int(self._node))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn off command to the switch."""
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn off switch {self._node.address}: {err}"
            ) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the turn on command to the switch."""
        try:
            await self._node.send_command(CMD_ON)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn on switch {self._node.address}: {err}"
            ) from err


class ISYGroupSwitchEntity(ISYGroupEntity, ISYSwitchEntityMixin):
    """Representation of an ISY group switch device.

    ``pyisyox.Group`` deliberately exposes no live ``status`` — groups
    don't carry wire-level state of their own. The is_on aggregation is
    derived on access from the controller's nodes registry via
    ``group_any_on`` (any member currently non-zero).
    """

    _node: Group
    _attr_icon: str = "mdi:google-circles-communities"

    @property
    def is_on(self) -> bool:
        """True iff any member node is currently on."""
        return self._node.group_any_on


class ISYSwitchEntity(ISYNodeEntity, ISYSwitchEntityMixin):
    """Representation of an ISY switch device."""

    _node: Node


class ISYSwitchProgramEntity(ISYProgramEntity, SwitchEntity):
    """A representation of an ISY program switch.

    ``status`` (``self._node``) is the program that drives ``is_on``.
    ``self._actions`` is the sibling program that runs on user input
    — its ``then`` clause turns the switch on; ``else`` turns it off.
    The status program flips back through the WS dispatcher; the
    optimistic local state is the ``then`` / ``else`` branch we just
    requested.
    """

    _actions: Program
    _attr_icon: str = "mdi:script-text-outline"  # Matches isy program icon

    @property
    def is_on(self) -> bool:
        """Get whether the ISY switch program is on."""
        return self._node.status

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Run the actions program's ``then`` clause."""
        await self._actions.run_then()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Run the actions program's ``else`` clause."""
        await self._actions.run_else()


class ISYEnableSwitchEntity(ISYNodeEntity, SwitchEntity):
    """A representation of an ISY enable/disable switch."""

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        control: str,
        unique_id: str,
        description: ISYSwitchEntityDescription,
        device_info: DeviceInfo | None,
    ) -> None:
        """Initialize the IoX enable switch entity."""
        super().__init__(
            isy_data,
            node=node,
            control=control,
            unique_id=unique_id,
            description=description,
            device_info=device_info,
        )
        self._attr_name = description.name  # Override super
        # Always available; must follow super().__init__ which sets node.enabled
        self._attr_available = True

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event — availability is always True for enable switches."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Get whether the ISY device is in the on state."""
        return bool(self._node.enabled)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn off command to the ISY switch."""
        if not await self._node.disable():
            raise HomeAssistantError(f"Unable to disable device {self._node.address}")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the turn on command to the ISY switch."""
        if not await self._node.enable():
            raise HomeAssistantError(f"Unable to enable device {self._node.address}")
