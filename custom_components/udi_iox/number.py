"""Support for ISY number entities."""

from __future__ import annotations

from collections.abc import Callable
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
from pyisyox import (
    Event,
    Node,
    NodeCommandError,
    NodePropertyValue,
    Variable,
)
from pyisyox.constants import (
    CMD_BACKLIGHT,
    PROP_ON_LEVEL,
    NodeChangeAction,
)

from .const import BACKLIGHT_MEMORY_FILTER, UOM_8_BIT_RANGE
from .entity import ISYNodeEntity, _resolve_device_info
from .models import IsyConfigEntry, IsyData

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
        step = 10 ** (-1 * node.prec)
        min_max = ISY_MAX_SIZE / (10**node.prec)
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
            "device_info": _resolve_device_info(device_info, node),
        }
        if control == CMD_BACKLIGHT:
            entities.append(ISYBacklightNumberEntity(**entity_init_info))
            continue
        entities.append(ISYAuxControlNumberEntity(**entity_init_info))
    async_add_entities(entities)


class ISYAuxControlNumberEntity(ISYNodeEntity, NumberEntity):
    """Representation of a ISY/IoX Aux Control Number entity.

    HA shows the entity as a 0-100 percentage slider regardless of the
    underlying device's encoding. The IoX editor that backs the control
    decides whether the controller wants the value as raw bytes
    (0-255, e.g. classic Insteon dimmers) or already as a percentage
    (0-100, e.g. KeypadDimmer_ADV, Z-Wave dimmers). At init we resolve
    the editor on this node's nodedef and cache its max, then scale
    on read and write accordingly.
    """

    _attr_mode = NumberMode.SLIDER
    _editor_max: float | None = None

    @property
    def _resolved_editor_max(self) -> float | None:
        """Return the editor's max for this node's control, or ``None``.

        Cached on first call so the per-event read path stays cheap.
        ``None`` means "couldn't resolve" — caller should fall back to
        passing the raw value through unscaled.
        """
        if self._editor_max is not None:
            return self._editor_max
        if (nodedef := self._node.nodedef) is None:
            return None
        if (prop := nodedef.properties.get(self._control)) is None:
            return None
        editor = self._isy_data.root.profile.find_editor(
            prop.editor_id, self._node.family_id, self._node.instance_id
        )
        if editor is None or not editor.ranges:
            return None
        self._editor_max = editor.ranges[0].max
        return self._editor_max

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the variable."""
        node_prop: NodePropertyValue = self._node.properties[self._control]
        if not node_prop.value:
            return None

        try:
            raw = int(float(node_prop.value))
        except (TypeError, ValueError):
            return None

        if self.entity_description.native_unit_of_measurement == PERCENTAGE:
            editor_max = self._resolved_editor_max
            # Controller reports the value in the editor's range:
            # > 100 → raw bytes (e.g. Insteon 0-255), scale to percent.
            # ≤ 100 → already percent (KeypadDimmer_ADV / Z-Wave), as-is.
            # Unresolved → fall back to the legacy uom-only heuristic.
            if editor_max is not None:
                if editor_max > 100:
                    return ranged_value_to_percentage(ON_RANGE, raw)
                return raw
            if node_prop.uom == UOM_8_BIT_RANGE:
                return ranged_value_to_percentage(ON_RANGE, raw)
        return raw

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if self.entity_description.native_unit_of_measurement == PERCENTAGE:
            # HA passes 0-100; scale into the editor's expected range
            # (0-255 raw for classic Insteon dimmers, 0-100 percentage
            # for KeypadDimmer_ADV / Z-Wave / etc). The editor's max is
            # the source of truth — falling back to no-scale if we
            # can't resolve it lets pyisyox surface the codec error.
            editor_max = self._resolved_editor_max
            if editor_max is not None and editor_max > 100:
                value = percentage_to_ranged_value(ON_RANGE, round(value))
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
    """IoX variable as a number entity.

    Reads / writes against the typed :class:`pyisyox.Variable` wrapper —
    ``self._node.value`` / ``self._node.init`` reflect the record in
    place, and the mutation coroutines on the wrapper handle the
    ``POST /api/variables/{type}/{id}`` round-trip plus the record
    update on success.

    Variable change frames ride on the unified event stream
    (control ``_1``, action ``"6"`` value / ``"7"`` init);
    :class:`IsyControllerEvents` parses the ``<var>`` payload off
    ``Event.event_info`` and fans out to per-(type, id) listeners.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _init_entity: bool
    _node: Variable
    entity_description: NumberEntityDescription

    def __init__(
        self,
        isy_data: IsyData,
        node: Variable,
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
        self._unsubscribers.append(
            self._isy_data.controller_events.subscribe_variable(
                self._node.type_id, self._node.id, self._on_variable_change
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Drop subscriptions, if any."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    @callback
    def _on_variable_change(self, value: int | None, init: int | None) -> None:
        """Push the new value to the entity registry.

        pyisyox's :class:`EventDispatcher` (PR #71) already overlays the
        ``<var><val>`` payload onto the underlying ``VariableRecord``
        before this listener fires, so reads from ``self._node.value`` /
        ``self._node.init`` already reflect the wire change. Only the
        HA state-write is left for us.

        The ``value`` / ``init`` kwargs stay on the signature for
        backward compat — they let the listener decide whether the
        frame was for this entity's slot without re-reading the wrapper.
        """
        if self._init_entity and init is None:
            return  # current-value frame; not for this entity
        if not self._init_entity and value is None:
            return  # init frame; not for this entity
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the variable."""
        return self._node.init if self._init_entity else self._node.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Get the state attributes for the device."""
        return {"last_edited": self._node.ts or None}

    async def async_set_native_value(self, value: float) -> None:
        """Write the variable value via its typed wrapper.

        The wrapper updates its own record on success so the next
        ``native_value`` read reflects the new state — no separate
        optimistic mutation needed.
        """
        try:
            if self._init_entity:
                await self._node.set_init(int(value))
            else:
                await self._node.set_value(int(value))
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Could not set variable {self._node.address} to {value}: {err}"
            ) from err
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
        self._attr_native_value: float | int | None = 0

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
        if memory != BACKLIGHT_MEMORY_FILTER.get(
            "memory"
        ) or cmd1 != BACKLIGHT_MEMORY_FILTER.get("cmd1"):
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
