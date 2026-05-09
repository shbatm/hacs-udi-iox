"""Support for ISY locks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import Node, NodeCommandError

from .entity import ISYNodeEntity, ISYProgramEntity, NodeEventType, node_status_int
from .models import IsyConfigEntry, IsyData, ProgramRecord
from .services import async_setup_lock_services

VALUE_TO_STATE = {0: False, 100: True}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ISY lock platform."""
    isy_data = entry.runtime_data
    devices: dict[str, DeviceInfo] = isy_data.devices
    entities: list[ISYLockEntity | ISYLockProgramEntity] = []
    for node in isy_data.nodes[Platform.LOCK]:
        entities.append(
            ISYLockEntity(
                isy_data, node=node, device_info=devices.get(node.primary_node)
            )
        )

    for name, status, actions in isy_data.programs[Platform.LOCK]:
        entities.append(ISYLockProgramEntity(isy_data, name, status, actions))

    async_add_entities(entities)
    async_setup_lock_services(hass)


class ISYLockEntity(ISYNodeEntity, LockEntity):
    """Representation of an ISY lock device."""

    _node: Node

    async def async_added_to_hass(self) -> None:
        """Subscribe to events and set initial state."""
        await super().async_added_to_hass()
        status = node_status_int(self._node)
        self._attr_is_locked = (
            VALUE_TO_STATE.get(status) if status is not None else None
        )

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event from the ISY Node."""
        status = node_status_int(self._node)
        self._attr_is_locked = (
            VALUE_TO_STATE.get(status) if status is not None else None
        )
        super().async_on_update(event, key)

    async def async_lock(self, **kwargs: Any) -> None:
        """Send the lock command."""
        try:
            await self._node.secure_lock()
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to lock device {self._node.address}: {err}"
            ) from err

    async def async_unlock(self, **kwargs: Any) -> None:
        """Send the unlock command."""
        try:
            await self._node.secure_unlock()
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to unlock device {self._node.address}: {err}"
            ) from err

    async def async_set_zwave_lock_user_code(self, user_num: int, code: int) -> None:
        """Set a user lock code for a Z-Wave Lock — not supported."""
        raise HomeAssistantError(
            "Z-Wave lock user-code services are not supported"
        )

    async def async_delete_zwave_lock_user_code(self, user_num: int) -> None:
        """Delete a user lock code for a Z-Wave Lock — not supported."""
        raise HomeAssistantError(
            "Z-Wave lock user-code services are not supported"
        )


class ISYLockProgramEntity(ISYProgramEntity, LockEntity):
    """Representation of a ISY lock program."""

    _actions: ProgramRecord

    async def async_added_to_hass(self) -> None:
        """Subscribe to events and set initial state."""
        await super().async_added_to_hass()
        self._attr_is_locked = bool(node_status_int(self._node))

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle the update event from the ISY Node."""
        self._attr_is_locked = bool(node_status_int(self._node))
        super().async_on_update(event, key)

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the device."""
        if not await self._actions.run_then():
            raise HomeAssistantError(f"Unable to lock device {self._node.address}")

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the device."""
        if not await self._actions.run_else():
            raise HomeAssistantError(f"Unable to unlock device {self._node.address}")
