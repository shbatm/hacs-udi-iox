"""Support for ISY covers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import Node, NodeCommandError, Program
from pyisyox.constants import CMD_OFF, CMD_ON, PROP_ON_LEVEL

from .const import UOM_8_BIT_RANGE
from .entity import (
    ISYNodeEntity,
    ISYProgramEntity,
    NodeEventType,
    _resolve_device_info,
    node_status_int,
)
from .models import IsyConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ISY cover platform."""
    isy_data = entry.runtime_data
    entities: list[ISYCoverEntity | ISYCoverProgramEntity] = []
    devices: dict[str, DeviceInfo] = isy_data.devices
    for node in isy_data.nodes[Platform.COVER]:
        entities.append(
            ISYCoverEntity(
                isy_data, node=node, device_info=_resolve_device_info(devices, node)
            )
        )

    for name, status, actions in isy_data.programs[Platform.COVER]:
        entities.append(ISYCoverProgramEntity(isy_data, name, status, actions))

    async_add_entities(entities)


class ISYCoverEntity(ISYNodeEntity, CoverEntity):
    """Representation of an ISY cover device."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )
    _node: Node

    def _update_cover_attrs(self) -> None:
        status = node_status_int(self._node)
        if status is None:
            self._attr_current_cover_position = None
            self._attr_is_closed = None
            return
        if self._node.status is not None and self._node.status.uom == UOM_8_BIT_RANGE:
            self._attr_current_cover_position = round(status * 100.0 / 255.0)
        else:
            self._attr_current_cover_position = int(sorted((0, status, 100))[1])
        self._attr_is_closed = status == 0

    async def async_added_to_hass(self) -> None:
        """Subscribe to events and set initial state."""
        await super().async_added_to_hass()
        self._update_cover_attrs()

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event from the ISY Node."""
        self._update_cover_attrs()
        super().async_on_update(event, key)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Send the open cover command."""
        try:
            await self._node.send_command(CMD_ON)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to open the cover {self._node.address}: {err}"
            ) from err

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Send the close cover command."""
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to close the cover {self._node.address}: {err}"
            ) from err

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        position = kwargs[ATTR_POSITION]
        # HA gives 0-100 position; scale to whatever the OL editor on
        # this node accepts. Editor max > 100 → raw byte editor (classic
        # Insteon shade/relay), scale to 1-255. Editor max ≤ 100 (or
        # unresolvable) → pass the percentage through.
        editor_max = self._editor_max_for(PROP_ON_LEVEL)
        if editor_max is not None and editor_max > 100:
            position = round(position * 255.0 / 100.0)
        try:
            await self._node.set_on_level(position)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to set cover {self._node.address} position: {err}"
            ) from err


class ISYCoverProgramEntity(ISYProgramEntity, CoverEntity):
    """Representation of an ISY cover program.

    Status program True → closed (matches the legacy v3 convention).
    Open / close run the actions program's ``then`` / ``else`` clauses
    respectively.
    """

    _actions: Program

    @property
    def is_closed(self) -> bool:
        return self._node.status

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._actions.run_then()

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._actions.run_else()
