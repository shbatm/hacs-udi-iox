"""Support for ISY fans."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    int_states_in_range,
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)
from pyisyox import Node
from pyisyox.constants import CMD_OFF, Protocol

from .const import _LOGGER
from .entity import ISYNodeEntity, ISYProgramEntity, NodeEventType, node_status_int
from .models import IsyConfigEntry, IsyData, ProgramRecord

SPEED_RANGE = (1, 255)  # off is not included


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ISY fan platform."""
    isy_data = entry.runtime_data
    devices: dict[str, DeviceInfo] = isy_data.devices
    entities: list[ISYFanEntity | ISYFanProgramEntity] = []

    for node in isy_data.nodes[Platform.FAN]:
        entities.append(
            ISYFanEntity(
                isy_data, node=node, device_info=devices.get(node.primary_node)
            )
        )

    for name, status, actions in isy_data.programs[Platform.FAN]:
        entities.append(ISYFanProgramEntity(isy_data, name, status, actions))

    async_add_entities(entities)


class ISYFanEntity(ISYNodeEntity, FanEntity):
    """Representation of an ISY fan device."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.TURN_ON
    )
    _node: Node

    def __init__(
        self, isy_data: IsyData, node: Node, device_info: DeviceInfo | None = None
    ) -> None:
        """Initialize the IoX fan entity."""
        super().__init__(isy_data, node=node, device_info=device_info)
        self._attr_speed_count = (
            3 if node.protocol == Protocol.INSTEON else int_states_in_range(SPEED_RANGE)
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to events and set initial state."""
        await super().async_added_to_hass()
        self._update_fan_attrs()

    def _update_fan_attrs(self) -> None:
        if node_status_int(self._node) is None:
            self._attr_is_on = None
            self._attr_percentage = None
        else:
            self._attr_is_on = bool(node_status_int(self._node) != 0)
            self._attr_percentage = ranged_value_to_percentage(
                SPEED_RANGE, node_status_int(self._node)
            )

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event from the ISY Node."""
        self._update_fan_attrs()
        super().async_on_update(event, key)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan to a speed percentage."""
        if percentage == 0:
            await self._node.send_command(CMD_OFF)
            return

        isy_speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        await self._node.set_on_level(isy_speed)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Send the turn on command."""
        await self.async_set_percentage(percentage or 67)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn off command."""
        await self._node.send_command(CMD_OFF)


class ISYFanProgramEntity(ISYProgramEntity, FanEntity):
    """Representation of an ISY fan program."""

    _attr_supported_features = FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON
    _attr_speed_count = int_states_in_range(SPEED_RANGE)
    _actions: ProgramRecord

    async def async_added_to_hass(self) -> None:
        """Subscribe to events and set initial state."""
        await super().async_added_to_hass()
        self._update_fan_attrs()

    def _update_fan_attrs(self) -> None:
        if node_status_int(self._node) is None:
            self._attr_is_on = None
            self._attr_percentage = None
        else:
            self._attr_is_on = bool(node_status_int(self._node) != 0)
            self._attr_percentage = ranged_value_to_percentage(
                SPEED_RANGE, float(node_status_int(self._node))
            )

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle the update event from the ISY Node."""
        self._update_fan_attrs()
        super().async_on_update(event, key)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn on command to ISY fan program."""
        if not await self._actions.run_then():
            _LOGGER.error("Unable to turn off the fan")

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Send the turn off command to ISY fan program."""
        if not await self._actions.run_else():
            _LOGGER.error("Unable to turn on the fan")
