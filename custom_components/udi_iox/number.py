"""Support for ISY number entities."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import (
    CONF_VARIABLES,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)
from collections.abc import Callable

from pyisyox import (
    Event,
    Node,
    NodeCommandError,
    NodePropertyValue,
)
from pyisyox.constants import (
    ATTR_ACTION,
    CMD_BACKLIGHT,
    PROP_ON_LEVEL,
    TAG_ADDRESS,
    UOM_PERCENTAGE,
    NodeChangeAction,
)

from .const import BACKLIGHT_MEMORY_FILTER, UOM_8_BIT_RANGE
from .entity import ISYNodeEntity, node_status_int
from .models import IsyConfigEntry, IsyData, VariableRecord

ISY_MAX_SIZE = (2**32) / 2
ON_RANGE = (1, 255)  # Off is not included
CONTROL_DESC = {
    PROP_ON_LEVEL: NumberEntityDescription(
        key=PROP_ON_LEVEL,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        native_min_value=1.0,
        native_max_value=100.0,
        native_step=1.0,
    ),
    CMD_BACKLIGHT: NumberEntityDescription(
        key=CMD_BACKLIGHT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0.0,
        native_max_value=100.0,
        native_step=1.0,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ISY/IoX number entities from config entry."""
    isy_data = config_entry.runtime_data
    device_info = isy_data.devices
    entities: list[
        ISYVariableNumberEntity | ISYAuxControlNumberEntity | ISYBacklightNumberEntity
    ] = []

    for node in isy_data.variables[Platform.NUMBER]:
        step = 10 ** (-1 * node.precision)
        min_max = ISY_MAX_SIZE / (10**node.precision)
        description = NumberEntityDescription(
            key=node.address,
            name=node.name,
            entity_registry_enabled_default=False,
            native_unit_of_measurement=None,
            native_step=step,
            native_min_value=-min_max,
            native_max_value=min_max,
        )
        description_init = replace(
            description,
            key=f"{node.address}_init",
            name=f"{node.name} Initial Value",
            entity_category=EntityCategory.CONFIG,
        )

        entities.append(
            ISYVariableNumberEntity(
                isy_data,
                node,
                unique_id=isy_data.uid_base(node),
                description=description,
                device_info=device_info[CONF_VARIABLES],
            )
        )
        entities.append(
            ISYVariableNumberEntity(
                isy_data,
                node=node,
                unique_id=f"{isy_data.uid_base(node)}_init",
                description=description_init,
                device_info=device_info[CONF_VARIABLES],
                init_entity=True,
            )
        )

    for node, control in isy_data.aux_properties[Platform.NUMBER]:
        entity_init_info = {
            "isy_data": isy_data,
            "node": node,
            "control": control,
            "unique_id": f"{isy_data.uid_base(node)}_{control}",
            "description": CONTROL_DESC[control],
            "device_info": device_info.get(node.primary_node),
        }
        if control == CMD_BACKLIGHT:
            entities.append(ISYBacklightNumberEntity(**entity_init_info))
            continue
        entities.append(ISYAuxControlNumberEntity(**entity_init_info))
    async_add_entities(entities)


class ISYAuxControlNumberEntity(ISYNodeEntity, NumberEntity):
    """Representation of a ISY/IoX Aux Control Number entity."""

    _attr_mode = NumberMode.SLIDER

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the variable."""
        node_prop: NodePropertyValue = self._node.properties[self._control]
        if node_prop.value is None:
            return None

        if (
            self.entity_description.native_unit_of_measurement == PERCENTAGE
            and node_prop.uom == UOM_8_BIT_RANGE  # Insteon 0-255
        ):
            return ranged_value_to_percentage(ON_RANGE, node_prop.value)
        return int(node_prop.value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        node_prop: NodePropertyValue = self._node.properties[self._control]

        if self.entity_description.native_unit_of_measurement == PERCENTAGE:
            value = (
                percentage_to_ranged_value(ON_RANGE, round(value))
                if node_prop.uom == UOM_8_BIT_RANGE
                else value
            )
        if self._control == PROP_ON_LEVEL:
            await self._node.set_on_level(int(value))
            return

        try:
            await self._node.send_command(self._control, value)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Could not set {self.name} to {value} for {self._node.address}: {err}"
            ) from err


class ISYVariableNumberEntity(NumberEntity):
    """Representation of an IoX variable as a number entity.

    Variables are exposed as raw dicts in pyisyox 6.0.0a1 — read via
    ``controller.variables[type][index]``, written via
    ``controller.set_variable_value(type, id, value)``. Variable change
    frames flow on the unified event stream (control ``_1``, action
    ``"6"``/``"7"``); :class:`IsyControllerEvents` extracts the payload
    from ``Event.event_info`` (added in pyisyox#58) and dispatches to
    per-(type, id) listeners, which is what this entity subscribes to.
    Pre-pyisyox#58 builds dispatch nothing for variables, so the entity
    falls back to the optimistic local update from
    :meth:`async_set_native_value`.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _init_entity: bool
    _node: VariableRecord
    entity_description: NumberEntityDescription

    def __init__(
        self,
        isy_data: IsyData,
        node: VariableRecord,
        unique_id: str,
        description: NumberEntityDescription,
        device_info: DeviceInfo,
        init_entity: bool = False,
    ) -> None:
        """Initialize the IoX variable number."""
        self._isy_data = isy_data
        self._node = node
        self.entity_description = description
        self._unsubscribers: list[Callable[[], None]] = []

        # Two entities are created for each variable: one for current value,
        # one for initial. Initial value entities are disabled by default.
        self._init_entity = init_entity
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Subscribe to this variable's change frames."""
        var_type = self._node.get("type")
        var_id = self._node.get("id")
        if not var_type or not var_id:
            return
        self._unsubscribers.append(
            self._isy_data.controller_events.subscribe_variable(
                var_type, var_id, self._on_variable_change
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Drop subscriptions, if any."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    @callback
    def _on_variable_change(self, value: int | None, init: int | None) -> None:
        """Mirror a variable-change frame into local state."""
        if self._init_entity:
            if init is None:
                # Frame was a current-value change; not for this entity.
                return
            self._node["init"] = init
        else:
            if value is None:
                return
            self._node["value"] = value
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the variable."""
        if self._init_entity:
            return self._node.get("init")
        return self._node.get("value")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Get the state attributes for the device."""
        return {"last_edited": self._node.get("last_edited")}

    async def async_set_native_value(self, value: float) -> None:
        """Write the variable value via the controller.

        pyisyox 6.0.0a1 doesn't yet dispatch variable-change events on
        the WS, so the entity optimistically mirrors the write into
        its local record + HA state. When pyisyox grows variable
        event routing, this falls through to a real subscription.
        """
        controller = self._isy_data.root
        var_type = self._node.get("type")
        var_id = self._node.get("id")
        if not var_type or not var_id:
            raise HomeAssistantError(
                f"Variable record is missing type/id: {self._node!r}"
            )
        try:
            if self._init_entity:
                await controller.set_variable_init(var_type, var_id, value)
            else:
                await controller.set_variable_value(var_type, var_id, value)
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Could not set variable {var_type}/{var_id} to {value}: {err}"
            ) from err
        # Optimistic — surface the new value immediately.
        self._node["init" if self._init_entity else "value"] = value
        self.async_write_ha_state()


class ISYBacklightNumberEntity(ISYNodeEntity, RestoreNumber):
    """Representation of a ISY/IoX Backlight Number entity."""

    _attr_assumed_state = True  # Backlight values aren't read from device

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        control: str,
        unique_id: str,
        description: NumberEntityDescription,
        device_info: DeviceInfo | None,
    ) -> None:
        """Initialize the IoX backlight number entity."""
        super().__init__(
            isy_data,
            node=node,
            control=control,
            unique_id=unique_id,
            description=description,
            device_info=device_info,
        )
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """Restore last value + subscribe to memory-write echoes."""
        await super().async_added_to_hass()
        if (
            (last_state := await self.async_get_last_state())
            and (last_number_data := await self.async_get_last_number_data())
            and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        ):
            self._attr_native_value = last_number_data.native_value

        self._unsubscribers.append(
            self._isy_data.controller_events.subscribe_node(
                self._node.address,
                NodeChangeAction.DEVICE_MEMORY,
                self._on_memory_write,
            )
        )

    @callback
    def _on_memory_write(self, event: Event) -> None:
        """Handle a memory-write echo (BACKLIGHT_MEMORY_FILTER scoped)."""
        memory = getattr(event, "memory", None)
        cmd1 = getattr(event, "cmd1", None)
        raw_value = getattr(event, "value", None)
        if memory != BACKLIGHT_MEMORY_FILTER.get("memory") or cmd1 != BACKLIGHT_MEMORY_FILTER.get(
            "cmd1"
        ):
            return
        if raw_value is None:
            return
        value = ranged_value_to_percentage((0, 127), raw_value)
        if value == self._attr_native_value:
            return
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # set_backlight resolves the editor (percentage or index style)
        # internally — caller passes a single value.
        try:
            await self._node.set_backlight(int(value))
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Could not set backlight to {value}% for {self._node.address}: {err}"
            ) from err
        self._attr_native_value = value
        self.async_write_ha_state()
