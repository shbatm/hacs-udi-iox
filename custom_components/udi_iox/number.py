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
from homeassistant.util.percentage import ranged_value_to_percentage
from pyisyox import (
    DeviceWriteAction,
    Event,
    Node,
    NodeCommandError,
    NodePropertyValue,
    Variable,
)
from pyisyox.constants import (
    CMD_BACKLIGHT,
    PROP_ON_LEVEL,
)

from .const import BACKLIGHT_MEMORY_FILTER
from .editor_classification import range_for_control, unit_for_uom
from .entity import ISYNodeEntity, _resolve_device_info
from .models import IsyConfigEntry, IsyData

ISY_MAX_SIZE = (2**32) / 2

#: Hand-tuned descriptions for the well-known aux controls — On Level
#: excludes 0 ("Off" lives on the controllable), and the percentage
#: framing is fixed regardless of the editor's reported UOM. Everything
#: else is built from the control's editor at setup time (see
#: ``_number_description``).
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


def _number_description(
    isy_data: IsyData, node: Node, control: str
) -> NumberEntityDescription:
    """Build a NumberEntityDescription for ``control``.

    Hand-tuned controls (``CONTROL_DESC``) win; otherwise the bounds,
    step and unit come from the control's editor range — ``min`` /
    ``max`` slider bounds, ``step`` (or ``10**-precision`` when the
    editor doesn't specify one), and the friendly unit for the range's
    UOM. Falls back to HA's defaults (0-100, step 1, no unit) when the
    editor can't be resolved.
    """
    if (desc := CONTROL_DESC.get(control)) is not None:
        return desc
    rng = range_for_control(isy_data.root, node, control)
    if rng is None:
        return NumberEntityDescription(
            key=control, entity_category=EntityCategory.CONFIG
        )
    step = rng.step if rng.step is not None else 10 ** (-max(0, rng.precision))
    return NumberEntityDescription(
        key=control,
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=unit_for_uom(rng.uom),
        native_min_value=float(rng.min) if rng.min is not None else 0.0,
        native_max_value=float(rng.max) if rng.max is not None else 100.0,
        native_step=step,
    )


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
            "description": _number_description(isy_data, node, control),
            "device_info": _resolve_device_info(device_info, node),
        }
        if control == CMD_BACKLIGHT:
            entities.append(ISYBacklightNumberEntity(**entity_init_info))
            continue
        entities.append(ISYAuxControlNumberEntity(**entity_init_info))
    async_add_entities(entities)


class ISYAuxControlNumberEntity(ISYNodeEntity, RestoreNumber):
    """Representation of a ISY/IoX Aux Control Number entity.

    Two value-source modes, picked from whether the control is a
    reported nodedef *property* (Insteon ``OL`` / ``RR`` are written via
    a command but the controller also reports the value) or a write-only
    *command* (a plugin setter with no backing property):

    * **Readback control** — ``native_value`` reads ``node.properties``;
      ``unknown`` until the controller reports a frame (the entity is
      subscribed). ``assumed_state`` is ``False``.
    * **Write-only control** — no readback; the value is whatever was
      last set (restored across restarts via :class:`RestoreNumber`).
      ``assumed_state`` is ``True``, matching the backlight entity.

    pyisyox normalises read values to the control's editor unit and
    appends that unit on writes (``/cmd/OL/75/51``), so the
    classic-Insteon ``OL``-reports-a-0-255-byte quirk is handled
    library-side — this entity works in the editor's units (percent for
    ``I_OL``) both directions, no scaling here.
    """

    _attr_mode = NumberMode.SLIDER

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the aux-control number entity."""
        super().__init__(*args, **kwargs)
        self._optimistic_value: float | int | None = None

    @property
    def _has_readback(self) -> bool:
        """True when the controller reports this control as a property."""
        nodedef = self._node.nodedef
        return nodedef is not None and self._control in nodedef.properties

    @property
    def assumed_state(self) -> bool:
        """A write-only control has no readback — its state is optimistic."""
        return not self._has_readback

    async def async_added_to_hass(self) -> None:
        """Subscribe to control events; restore the last set value for a
        write-only control."""
        await super().async_added_to_hass()
        if not self._has_readback and (last := await self.async_get_last_number_data()):
            self._optimistic_value = last.native_value

    @property
    def native_value(self) -> float | int | None:
        """Return the entity's current value."""
        if not self._has_readback:
            return self._optimistic_value

        node_prop: NodePropertyValue | None = self._node.properties.get(self._control)
        if node_prop is None or not node_prop.value:
            return None

        # pyisyox already normalised the value to the control's editor
        # unit (e.g. a classic-Insteon ``OL`` 0-255 byte → 0-100%).
        try:
            return int(float(node_prop.value))
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # The value is in the control's editor unit (percent for
        # ``I_OL``); pyisyox appends the UOM on the wire and the
        # controller does any device-side scaling.
        if self._control == PROP_ON_LEVEL:
            try:
                await self._node.set_on_level(int(value))
            except NodeCommandError as err:
                raise HomeAssistantError(
                    f"Could not set {self.name} to {value} for "
                    f"{self._node.address}: {err}"
                ) from err
        else:
            try:
                await self._node.send_command(self._control, value)
            except NodeCommandError as err:
                raise HomeAssistantError(
                    f"Could not set {self.name} to {value} for "
                    f"{self._node.address}: {err}"
                ) from err
        if not self._has_readback:
            self._optimistic_value = value
            self.async_write_ha_state()


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
        """Return the displayed (precision-scaled) state of the variable.

        IoX variables store an integer raw value on the wire; the
        ``precision`` field declares the implicit decimal shift. The
        entity's ``native_step`` / ``native_min_value`` / ``native_max_value``
        are computed in displayed units (set up in ``async_setup_entry``),
        so the read side has to match: ``raw / 10**precision``.
        """
        raw = self._node.init if self._init_entity else self._node.value
        if raw is None:
            return None
        precision = self._node.precision or 0
        if precision <= 0:
            return raw
        return raw / (10**precision)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Get the state attributes for the device."""
        return {"last_edited": self._node.ts or None}

    async def async_set_native_value(self, value: float) -> None:
        """Write the variable value via its typed wrapper.

        ``value`` arrives in *displayed* units (HA's slider / number
        widget speaks the same scale as ``native_step``). The modern
        ``POST /api/variables/{type}/{id}`` endpoint accepts both
        ``int`` and ``float`` and applies the ``* 10**precision`` shift
        server-side when the body is a float — so the displayed value
        is passed straight through. (Rounding to int here would lose
        the fractional portion **and** misalign with the controller's
        precision math: an int body is stored verbatim, no scaling.)
        """
        try:
            if self._init_entity:
                await self._node.set_init(value)
            else:
                await self._node.set_value(value)
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
                DeviceWriteAction.MEMORY,
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
