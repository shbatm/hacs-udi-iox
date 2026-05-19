"""Support for ISY lights."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from pyisyox import Group, Node, NodeCommandError
from pyisyox.constants import CMD_OFF, CMD_ON

from .const import CONF_RESTORE_LIGHT_STATE, UOM_PERCENTAGE
from .editor_classification import resolve_editor
from .entity import (
    ISYGroupEntity,
    ISYNodeEntity,
    NodeEventType,
    _group_device_info,
    _resolve_device_info,
    node_status_int,
)
from .models import IsyConfigEntry, IsyData

ATTR_LAST_BRIGHTNESS = "last_brightness"

PARALLEL_UPDATES = 0


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

    entities: list[ISYLightEntity | ISYGroupLightEntity] = []
    for node in isy_data.nodes[Platform.LIGHT]:
        entities.append(
            ISYLightEntity(
                isy_data, node, restore_light_state, _resolve_device_info(devices, node)
            )
        )

    # State-maintained scenes with a dimmable member (hacs-udi-iox#86) —
    # on/off light (no brightness; scenes carry no settable level).
    for group in isy_data.group_lights:
        entities.append(
            ISYGroupLightEntity(
                isy_data,
                node=group,
                device_info=_group_device_info(isy_data, group, devices),
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
        status = node_status_int(self._node)
        if status is None:
            return False
        return status != 0

    @property
    def brightness(self) -> int | None:
        """Get the brightness of the ISY light."""
        status = node_status_int(self._node)
        if status is None:
            return None
        # Special Case for ISY Z-Wave Devices using % instead of 0-255:
        if self._node.status is not None and self._node.status.uom == UOM_PERCENTAGE:
            return round(status * 255.0 / 100.0)
        return status

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn off command to the light device."""
        self._last_brightness = self.brightness
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn off light {self._node.address}: {err}"
            ) from err

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Save brightness in the update event from the ISY Node."""
        status = node_status_int(self._node)
        if status:  # Not 0 or None
            if (
                self._node.status is not None
                and self._node.status.uom == UOM_PERCENTAGE
            ):
                self._last_brightness = round(status * 255.0 / 100.0)
            else:
                self._last_brightness = status
        super().async_on_update(event, key)

    async def async_turn_on(self, brightness: int | None = None, **kwargs: Any) -> None:
        """Send the turn on command to the light device.

        Brightness is set via the ``DON`` command's level parameter
        (``/cmd/DON/<v>/<uom>``) for every dimmer family — that's the
        controller's "go to this level now" surface. (Insteon's separate
        ``OL`` "On Level" — the level a bare paddle press goes to — is a
        device *setting*, surfaced as its own NUMBER entity, not touched
        here.)
        """
        if self._restore_light_state and brightness is None and self._last_brightness:
            brightness = self._last_brightness
        if brightness is not None:
            # Scale 0-255 → 0-100 when the DON editor expects percent
            # (uom 51) or a byte-capped 0-100 subset (uom 100, max ≤ 100);
            # full-range 0-255 dimmers pass through.
            editor = resolve_editor(self._isy_data.root, self._node, CMD_ON)
            if editor is not None:
                rng = editor.range_for(UOM_PERCENTAGE)
                if rng.uom == UOM_PERCENTAGE or (
                    rng.max is not None and rng.max <= 100
                ):
                    brightness = round(brightness * 100.0 / 255.0)
        try:
            if brightness is None:
                await self._node.send_command(CMD_ON)
            else:
                await self._node.send_command(CMD_ON, brightness)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn on light {self._node.address}: {err}"
            ) from err

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


class ISYGroupLightEntity(ISYGroupEntity, LightEntity):
    """A state-maintained IoX scene with a dimmable member, as a light.

    Dimmable scenes are routed to the light domain natively (instead of
    switch) so they keep light semantics + the scene-member more-info
    framework without a ``switch_as_x`` wrapper. Scenes carry **no**
    settable brightness — fade/brt/dim are separate manual commands, not
    a scene level — so this is on/off only. State mirrors the switch
    counterpart's ``group_any_on``, kept live by the pyisyox member→group
    event re-emit (hacs-udi-iox#86).
    """

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _node: Group
    _attr_icon = "mdi:google-circles-communities"

    @property
    def is_on(self) -> bool:
        """True iff any member node is currently on."""
        return self._node.group_any_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate the scene (broadcast its On command to all members)."""
        try:
            await self._node.send_command(CMD_ON)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn on scene {self._node.address}: {err}"
            ) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Deactivate the scene."""
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn off scene {self._node.address}: {err}"
            ) from err
