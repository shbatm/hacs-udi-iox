"""Support for ISY locks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import Node, NodeCommandError, Program

from .entity import (
    ISYNodeEntity,
    ISYProgramEntity,
    NodeEventType,
    _resolve_device_info,
    node_status_int,
)
from .models import IsyConfigEntry

VALUE_TO_STATE = {0: False, 100: True}

PARALLEL_UPDATES = 0


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
                isy_data, node=node, device_info=_resolve_device_info(devices, node)
            )
        )

    for name, status, actions in isy_data.programs[Platform.LOCK]:
        entities.append(ISYLockProgramEntity(isy_data, name, status, actions))

    async_add_entities(entities)


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
        raise HomeAssistantError("Z-Wave lock user-code services are not supported")

    async def async_delete_zwave_lock_user_code(self, user_num: int) -> None:
        """Delete a user lock code for a Z-Wave Lock — not supported."""
        raise HomeAssistantError("Z-Wave lock user-code services are not supported")


class ISYLockProgramEntity(ISYProgramEntity, LockEntity):
    """Representation of a ISY lock program."""

    _actions: Program

    @property
    def is_locked(self) -> bool:
        """Lock state — True when the program's status program is True."""
        return self._node.status

    async def async_lock(self, **kwargs: Any) -> None:
        """Run the actions program's ``then`` clause to lock."""
        try:
            await self._actions.run_then()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to lock program {self._node.address}: {err}"
            ) from err

    async def async_unlock(self, **kwargs: Any) -> None:
        """Run the actions program's ``else`` clause to unlock."""
        try:
            await self._actions.run_else()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to unlock program {self._node.address}: {err}"
            ) from err
