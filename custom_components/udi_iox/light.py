"""Support for ISY lights."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from pyisyox import Node, NodeCommandError
from pyisyox.constants import CMD_OFF, CMD_ON

from .const import _LOGGER, CONF_RESTORE_LIGHT_STATE, UOM_PERCENTAGE
from .entity import ISYNodeEntity, NodeEventType, node_status_int
from .models import IsyConfigEntry, IsyData

ATTR_LAST_BRIGHTNESS = "last_brightness"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ISY light platform."""
    isy_data = entry.runtime_data
    devices: dict[str, DeviceInfo] = isy_data.devices
    isy_options = entry.options
    restore_light_state = isy_options.get(CONF_RESTORE_LIGHT_STATE, False)

    entities = []
    for node in isy_data.nodes[Platform.LIGHT]:
        entities.append(
            ISYLightEntity(
                isy_data, node, restore_light_state, devices.get(node.primary_node)
            )
        )

    async_add_entities(entities)


class ISYLightEntity(ISYNodeEntity, LightEntity, RestoreEntity):
    """Representation of an ISY light device."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _node: Node

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        restore_light_state: bool,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the IoX light device."""
        super().__init__(isy_data, node, device_info=device_info)
        self._last_brightness: int | None = None
        self._restore_light_state = restore_light_state

    @property
    def is_on(self) -> bool:
        """Get whether the ISY light is on."""
        if node_status_int(self._node) is None:
            return False
        return int(node_status_int(self._node)) != 0

    @property
    def brightness(self) -> int | None:
        """Get the brightness of the ISY light."""
        if node_status_int(self._node) is None:
            return None
        # Special Case for ISY Z-Wave Devices using % instead of 0-255:
        if (self._node.status is not None and self._node.status.uom == UOM_PERCENTAGE):
            return round(cast(float, node_status_int(self._node)) * 255.0 / 100.0)
        return int(node_status_int(self._node))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn off command to the light device."""
        self._last_brightness = self.brightness
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            _LOGGER.debug("Unable to turn off light: %s", err)

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Save brightness in the update event from the ISY Node."""
        if node_status_int(self._node):  # Not 0 or None
            self._last_brightness = node_status_int(self._node)
            if (self._node.status is not None and self._node.status.uom == UOM_PERCENTAGE):
                self._last_brightness = round(node_status_int(self._node) * 255.0 / 100.0)
            else:
                self._last_brightness = node_status_int(self._node)
        super().async_on_update(event, key)

    async def async_turn_on(self, brightness: int | None = None, **kwargs: Any) -> None:
        """Send the turn on command to the light device."""
        if self._restore_light_state and brightness is None and self._last_brightness:
            brightness = self._last_brightness
        # Z-Wave dimmers report uom as percent (0-100); convert from
        # HA's 0-255 brightness range before handing the value to
        # the editor-validated set_on_level wrapper.
        if brightness is not None and (self._node.status is not None and self._node.status.uom == UOM_PERCENTAGE):
            brightness = round(brightness * 100.0 / 255.0)
        try:
            if brightness is None:
                await self._node.send_command(CMD_ON)
            else:
                await self._node.set_on_level(brightness)
        except NodeCommandError as err:
            _LOGGER.debug("Unable to turn on light: %s", err)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the light attributes."""
        return {ATTR_LAST_BRIGHTNESS: self._last_brightness}

    async def async_added_to_hass(self) -> None:
        """Restore last_brightness on restart."""
        await super().async_added_to_hass()

        self._last_brightness = self.brightness or 255
        if not (last_state := await self.async_get_last_state()):
            return

        if last_state.attributes.get(ATTR_LAST_BRIGHTNESS):
            self._last_brightness = last_state.attributes[ATTR_LAST_BRIGHTNESS]
