"""Support for ISY select entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from pyisyox import (
    DeviceWriteAction,
    Event,
    Node,
    NodeCommandError,
    NodePropertyValue,
)
from pyisyox.constants import (
    CMD_BACKLIGHT,
    INSTEON_RAMP_RATES,
    PROP_RAMP_RATE,
    UOM_TO_STATES,
)

from .const import _LOGGER, BACKLIGHT_MEMORY_FILTER
from .editor_classification import range_for_control
from .entity import ISYNodeEntity, _resolve_device_info
from .models import IsyConfigEntry, IsyData


def time_string(i: float) -> str:
    """Return a formatted ramp rate time string."""
    if i >= 60.0:
        return f"{(i / 60.0):.1f} {UnitOfTime.MINUTES}"
    return f"{i} {UnitOfTime.SECONDS}"


RAMP_RATE_OPTIONS = [time_string(rate) for rate in INSTEON_RAMP_RATES.values()]


def _select_options(isy_data: IsyData, node: Node, control: str) -> list[str]:
    """Resolve the option list for an aux SELECT control.

    The control's *editor* is the source of truth: ``names`` (narrowed
    by ``subset`` — only the listed raw ints are valid, including combo
    specs like ``0-2,6,7``) gives the option strings in raw-int order.
    That covers the keypad backlight too (``I_BL_KP`` carries the full
    0-127 "On N / Off M" ``names`` table). ``UOM_TO_STATES`` is the
    fallback for the few index UOMs that lean on the global table rather
    than per-editor names (slated to be retired once everything resolves
    from editors); RAMP_RATE keeps its bespoke table.
    """
    if control == PROP_RAMP_RATE:
        return RAMP_RATE_OPTIONS
    rng = range_for_control(isy_data.root, node, control)
    if rng is not None and rng.names:
        keys = sorted(rng.subset) if rng.subset else sorted(rng.names)
        return [rng.names[k] for k in keys if k in rng.names]
    prop = node.properties.get(control)
    if prop is not None and (options_dict := UOM_TO_STATES.get(prop.uom)):
        return [str(value) for value in options_dict.values()]
    return []


PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ISY/IoX select entities from config entry."""
    isy_data = config_entry.runtime_data
    device_info = isy_data.devices
    entities: list[
        ISYAuxControlIndexSelectEntity
        | ISYRampRateSelectEntity
        | ISYBacklightSelectEntity
    ] = []

    for node, control in isy_data.aux_properties[Platform.SELECT]:
        # The entity's friendly name (incl. the sub-node prefix where
        # one's needed) is composed by ISYNodeEntity from the nodedef
        # label — don't set ``name`` here, it'd be ignored.
        options = _select_options(isy_data, node, control)
        description = SelectEntityDescription(
            key=f"{node.address}_{control}",
            entity_category=EntityCategory.CONFIG,
            options=options,
        )
        entity_detail: dict = {
            "isy_data": isy_data,
            "node": node,
            "control": control,
            "unique_id": f"{isy_data.uid_base(node)}_{control}",
            "description": description,
            "device_info": _resolve_device_info(device_info, node),
        }

        if control == PROP_RAMP_RATE:
            entities.append(ISYRampRateSelectEntity(**entity_detail))
            continue
        if control == CMD_BACKLIGHT:
            entities.append(ISYBacklightSelectEntity(**entity_detail))
            continue
        if not options:
            # No editor names and no UOM_TO_STATES entry — nothing to
            # show. The send_node_command service still reaches it.
            _LOGGER.debug(
                "No select options resolved for %s/%s; skipping", node.name, control
            )
            continue
        entities.append(ISYAuxControlIndexSelectEntity(**entity_detail))
    async_add_entities(entities)


class ISYRampRateSelectEntity(ISYNodeEntity, SelectEntity):
    """Representation of a ISY/IoX Aux Control Ramp Rate Select entity."""

    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        node_prop: NodePropertyValue | None = self._node.properties.get(self._control)
        if node_prop is None or node_prop.value is None:
            return None
        try:
            return RAMP_RATE_OPTIONS[int(float(node_prop.value))]
        except (IndexError, TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self._node.set_ramp_rate(RAMP_RATE_OPTIONS.index(option))


class ISYAuxControlIndexSelectEntity(ISYNodeEntity, SelectEntity, RestoreEntity):
    """Representation of a ISY/IoX Aux Control Index/enum Select entity.

    Like the aux number entity, two value-source modes:

    * **Readback control** — a reported nodedef property: ``current_option``
      maps the live raw value to its option string (via ``UOM_TO_STATES``
      or the editor's ``names``). ``unknown`` until reported.
    * **Write-only control** — a plugin enum setter with no backing
      property: ``current_option`` is the last selected option (restored
      across restarts). ``assumed_state`` is ``True``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the aux index/enum select entity."""
        super().__init__(*args, **kwargs)
        self._optimistic_option: str | None = None

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
        """Subscribe to control events; restore the last selected option
        for a write-only control."""
        await super().async_added_to_hass()
        if (
            not self._has_readback
            and (last := await self.async_get_last_state()) is not None
            and last.state in self.options
        ):
            self._optimistic_option = last.state

    @property
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        if not self._has_readback:
            return self._optimistic_option

        node_prop: NodePropertyValue | None = self._node.properties.get(self._control)
        if node_prop is None or node_prop.value is None:
            return None

        if options_dict := UOM_TO_STATES.get(node_prop.uom):
            return options_dict.get(str(node_prop.value), str(node_prop.value))
        # Editor names: map the raw int → option string the way the
        # options list was built (sorted by raw int).
        rng = self._editor_range_for(self._control)
        if rng is not None and rng.names:
            try:
                return rng.names[int(float(node_prop.value))]
            except (KeyError, TypeError, ValueError):
                pass
        return node_prop.formatted or None

    async def async_select_option(self, option: str) -> None:
        """Change the selected option.

        Sends the friendly option string — the editor codec on
        ``send_command`` maps an enum name back to its raw int (so a
        sparse / subset-narrowed ``names`` table round-trips correctly,
        unlike a positional index would).
        """
        try:
            await self._node.send_command(self._control, option)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Could not set {self.name} to {option!r} for "
                f"{self._node.address}: {err}"
            ) from err
        if not self._has_readback:
            self._optimistic_option = option
            self.async_write_ha_state()


class ISYBacklightSelectEntity(ISYNodeEntity, SelectEntity, RestoreEntity):
    """Representation of a ISY/IoX Backlight Select entity."""

    _attr_assumed_state = True  # Backlight values aren't read from device

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        control: str,
        unique_id: str,
        description: SelectEntityDescription,
        device_info: DeviceInfo | None,
    ) -> None:
        """Initialize the IoX Backlight Select entity."""
        super().__init__(
            isy_data,
            node=node,
            control=control,
            unique_id=unique_id,
            description=description,
            device_info=device_info,
        )
        self._attr_current_option: str | None = None

    async def async_added_to_hass(self) -> None:
        """Load the last known state and watch for memory-write echoes."""
        await super().async_added_to_hass()
        if (
            last_state := await self.async_get_last_state()
        ) and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._attr_current_option = last_state.state

        # The Insteon backlight memory write echoes back as a control
        # event with the wire code "_7M" (DeviceWriteAction.MEMORY).
        # Subscribe to that control on this node and filter inside the
        # callback — rare enough that the cost is irrelevant.
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
        # Without per-event_info filtering we'd react to every memory
        # write on the node; check the wire-level memory address +
        # cmd1 byte the legacy filter used.
        memory = getattr(event, "memory", None)
        cmd1 = getattr(event, "cmd1", None)
        value = getattr(event, "value", None)
        if memory != BACKLIGHT_MEMORY_FILTER.get(
            "memory"
        ) or cmd1 != BACKLIGHT_MEMORY_FILTER.get("cmd1"):
            return
        if value is None:
            return
        rng = self._editor_range_for(self._control)
        try:
            raw = int(value)
            option = rng.names[raw] if rng is not None and rng.names else str(raw)
        except (KeyError, TypeError, ValueError):
            return
        if option == self._attr_current_option:
            return
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected backlight option."""
        # set_backlight handles index-style editors directly — accepts
        # the friendly option name when the editor's enum table has it.
        try:
            await self._node.set_backlight(option)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Could not set backlight to {option} for {self._node.address}: {err}"
            ) from err
        self._attr_current_option = option
        self.async_write_ha_state()
