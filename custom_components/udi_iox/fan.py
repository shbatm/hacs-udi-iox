"""Support for ISY fans."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    int_states_in_range,
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)
from pyisyox import Node, NodeCommandError, Program
from pyisyox.constants import CMD_OFF, CMD_ON, PROP_STATUS

from .entity import (
    ISYNodeEntity,
    ISYProgramEntity,
    NodeEventType,
    _resolve_device_info,
    node_status_int,
)
from .models import IsyConfigEntry, IsyData

#: Used only when the fan node's ``ST`` editor doesn't give us a better
#: bound (no ``max``, no enumerated speeds). IoX 6 expresses fan speed in
#: the ``ST`` editor's unit — for the classic Insteon FanLinc that's
#: ``I_FLM_LVL`` (UOM 51, 0-100% with discrete steps); for a continuous
#: dimmer-style fan it's a 0-100 (or editor-``max``) range.
SPEED_RANGE_FALLBACK = (1, 100)  # off is not included


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
                isy_data, node=node, device_info=_resolve_device_info(devices, node)
            )
        )

    for name, status, actions in isy_data.programs[Platform.FAN]:
        entities.append(ISYFanProgramEntity(isy_data, name, status, actions))

    async_add_entities(entities)


class ISYFanEntity(ISYNodeEntity, FanEntity):
    """Representation of an ISY fan device.

    Speed handling is driven by the node's ``ST`` editor:

    * **Enumerated** (``subset`` / ``names`` — e.g. the Insteon FanLinc's
      ``I_FLM_LVL`` ``0,25,75,100`` ⇒ Off/Low/Medium/High) — the on
      values become an ordered preset list; HA's percentage maps to the
      nearest member, so a command never sends an off-list value the
      controller would reject.
    * **Continuous** (``min``/``max``) — a plain ranged scale.

    Read values come pre-normalised to the editor's unit by pyisyox, and
    a set command is sent in that same unit (``/cmd/DON/{value}/{uom}``),
    so there's no 0-255↔0-100 juggling here.
    """

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
        rng = self._editor_range_for(PROP_STATUS)
        self._ordered_speeds: list[int] | None = None
        self._speed_range: tuple[int, int] = SPEED_RANGE_FALLBACK
        if rng is not None:
            enumerated = rng.subset or set(rng.names)
            on_speeds = sorted(v for v in enumerated if v)
            if on_speeds:
                self._ordered_speeds = on_speeds
            elif rng.max is not None:
                self._speed_range = (1, int(rng.max))
        self._attr_speed_count = (
            len(self._ordered_speeds)
            if self._ordered_speeds is not None
            else int_states_in_range(self._speed_range)
        )

    def _value_to_percentage(self, value: int) -> int:
        if value <= 0:
            return 0
        if self._ordered_speeds is not None:
            nearest = min(self._ordered_speeds, key=lambda s: abs(s - value))
            return ordered_list_item_to_percentage(self._ordered_speeds, nearest)
        return ranged_value_to_percentage(self._speed_range, value)

    async def async_added_to_hass(self) -> None:
        """Subscribe to events and set initial state."""
        await super().async_added_to_hass()
        self._update_fan_attrs()

    def _update_fan_attrs(self) -> None:
        status = node_status_int(self._node)
        if status is None:
            self._attr_is_on = None
            self._attr_percentage = None
        else:
            self._attr_is_on = status != 0
            self._attr_percentage = self._value_to_percentage(status)

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event from the ISY Node."""
        self._update_fan_attrs()
        super().async_on_update(event, key)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan to a speed percentage."""
        try:
            if percentage == 0:
                await self._node.send_command(CMD_OFF)
                return
            if self._ordered_speeds is not None:
                value = percentage_to_ordered_list_item(
                    self._ordered_speeds, percentage
                )
            else:
                value = math.ceil(
                    percentage_to_ranged_value(self._speed_range, percentage)
                )
            await self._node.send_command(CMD_ON, value)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to set fan speed on {self._node.address}: {err}"
            ) from err

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
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn off fan {self._node.address}: {err}"
            ) from err


class ISYFanProgramEntity(ISYProgramEntity, FanEntity):
    """Representation of an ISY fan program (on/off only).

    Programs only carry boolean status, so program-driven fans
    don't expose a percentage / speed control. Multi-speed fans
    should be wired via a Node FAN entity instead.
    """

    _attr_supported_features = FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON
    _actions: Program

    @property
    def is_on(self) -> bool:
        return self._node.status

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Run the actions program's ``then`` clause."""
        try:
            await self._actions.run_then()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to turn on fan program {self._node.address}: {err}"
            ) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Run the actions program's ``else`` clause."""
        try:
            await self._actions.run_else()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to turn off fan program {self._node.address}: {err}"
            ) from err
