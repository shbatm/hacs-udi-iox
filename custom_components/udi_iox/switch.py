"""Support for ISY switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import (
    STATE_ON,
    EntityCategory,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from pyisyox import Group, Node, NodeCommandError, NodePropertyValue, Program
from pyisyox.constants import CMD_OFF, CMD_ON, TAG_ENABLED

from .entity import (
    ISYGroupEntity,
    ISYNodeEntity,
    ISYProgramEntity,
    NodeEventType,
    _resolve_device_info,
    aux_entity_category,
    node_status_int,
)
from .models import IsyConfigEntry, IsyData
from .program_device import (
    PROGRAM_ENABLE_SWITCH_SUFFIX,
    PROGRAM_RUN_AT_STARTUP_SWITCH_SUFFIX,
    ISYProgramDeviceEntity,
)

PARALLEL_UPDATES = 0


@dataclass
class ISYSwitchEntityDescription(SwitchEntityDescription):
    """Describes IST switch."""

    # ISYEnableSwitchEntity does not support UNDEFINED or None,
    # restrict the type to str.
    name: str = ""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ISY switch platform."""
    isy_data = entry.runtime_data
    entities: list[
        ISYSwitchEntity
        | ISYGroupSwitchEntity
        | ISYSwitchProgramEntity
        | ISYEnableSwitchEntity
        | ISYAuxControlSwitchEntity
    ] = []
    device_info = isy_data.devices
    for node in isy_data.nodes[Platform.SWITCH]:
        entities.append(
            ISYSwitchEntity(
                isy_data,
                node=node,
                device_info=_resolve_device_info(device_info, node),
            )
        )

    for group in isy_data.groups:
        device = None
        if group.controller_addresses and len(group.controller_addresses) == 1:
            # If Group has only one controller, link to that device
            # instead of the hub.
            primary_addr = group.controller_addresses[0]
            controller_node = isy_data.root.nodes.get(primary_addr)
            if controller_node is not None:
                device = _resolve_device_info(device_info, controller_node)
        entities.append(ISYGroupSwitchEntity(isy_data, node=group, device_info=device))

    for name, status, actions in isy_data.programs[Platform.SWITCH]:
        entities.append(ISYSwitchProgramEntity(isy_data, name, status, actions))

    for program in isy_data.program_devices:
        program_device_info = device_info.get(f"program_{program.address}")
        if program_device_info is None:
            continue
        entities.append(ISYProgramEnableSwitch(isy_data, program, program_device_info))
        entities.append(
            ISYProgramRunAtStartupSwitch(isy_data, program, program_device_info)
        )

    for node, control in isy_data.aux_properties[Platform.SWITCH]:
        unique_id = f"{isy_data.uid_base(node)}_{control}"
        resolved_device_info = _resolve_device_info(device_info, node)
        if control == TAG_ENABLED:
            # The node-lifecycle enable/disable flag — bespoke entity
            # (reads ``node.enabled``, sends ``Node.set_enabled``).
            entities.append(
                ISYEnableSwitchEntity(
                    isy_data,
                    node=node,
                    control=control,
                    unique_id=unique_id,
                    description=ISYSwitchEntityDescription(
                        key=control,
                        device_class=SwitchDeviceClass.SWITCH,
                        name=control.title(),
                        entity_category=EntityCategory.CONFIG,
                    ),
                    device_info=resolved_device_info,
                )
            )
            continue
        # A coalesced boolean aux control (Insteon i3 ``*Flags`` GVx):
        # a binary-editor accept command paired with its readback property.
        entities.append(
            ISYAuxControlSwitchEntity(
                isy_data,
                node=node,
                control=control,
                unique_id=unique_id,
                device_info=resolved_device_info,
            )
        )
    async_add_entities(entities)


class ISYSwitchEntityMixin(SwitchEntity):
    """Representation of an ISY switch device."""

    _node: Node | Group

    @property
    def is_on(self) -> bool | None:
        """Get whether the ISY device is in the on state."""
        if node_status_int(self._node) is None:
            return None
        return bool(node_status_int(self._node))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the turn off command to the switch."""
        try:
            await self._node.send_command(CMD_OFF)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn off switch {self._node.address}: {err}"
            ) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the turn on command to the switch."""
        try:
            await self._node.send_command(CMD_ON)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to turn on switch {self._node.address}: {err}"
            ) from err


class ISYGroupSwitchEntity(ISYGroupEntity, ISYSwitchEntityMixin):
    """Group switch — ``is_on`` derives from ``group_any_on`` (any
    member non-zero) since groups carry no wire-level status."""

    _node: Group
    _attr_icon: str = "mdi:google-circles-communities"

    @property
    def is_on(self) -> bool:
        """True iff any member node is currently on."""
        return self._node.group_any_on


class ISYSwitchEntity(ISYNodeEntity, ISYSwitchEntityMixin):
    """Representation of an ISY switch device."""

    _node: Node


class ISYSwitchProgramEntity(ISYProgramEntity, SwitchEntity):
    """ISY program switch — status program drives ``is_on``;
    actions program's ``then`` / ``else`` runs on user input."""

    _actions: Program
    _attr_icon: str = "mdi:script-text-outline"  # Matches isy program icon

    @property
    def is_on(self) -> bool:
        """Get whether the ISY switch program is on."""
        return self._node.status

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Run the actions program's ``then`` clause."""
        try:
            await self._actions.run_then()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to turn on switch program {self._node.address}: {err}"
            ) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Run the actions program's ``else`` clause."""
        try:
            await self._actions.run_else()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to turn off switch program {self._node.address}: {err}"
            ) from err


class ISYEnableSwitchEntity(ISYNodeEntity, SwitchEntity):
    """A representation of an ISY enable/disable switch."""

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        control: str,
        unique_id: str,
        description: ISYSwitchEntityDescription,
        device_info: DeviceInfo | None,
    ) -> None:
        """Initialize the IoX enable switch entity."""
        super().__init__(
            isy_data,
            node=node,
            control=control,
            unique_id=unique_id,
            description=description,
            device_info=device_info,
        )
        self._attr_name = description.name  # Override super

    @property
    def available(self) -> bool:
        """The enable switch ignores ``node.enabled`` (else there'd be
        no way to re-enable a disabled node), but still respects WS
        health — when the event stream is down the controller is
        unreachable and the command would fail anyway."""
        return self._isy_data.controller_events.ws_connected

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Reflect a control / lifecycle event in the switch state."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """``node.enabled`` tracks ``EN`` lifecycle frames — also picks
        up admin-console / REST toggles."""
        return bool(self._node.enabled)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the node on the controller."""
        await self._async_set_enabled(False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Re-enable the node on the controller."""
        await self._async_set_enabled(True)

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Toggle the node's controller-side enabled flag.

        ``Node.set_enabled`` updates the node record optimistically on
        success, so ``is_on`` reflects the change immediately; the
        controller also pushes a lifecycle ``EN`` event.
        """
        try:
            await self._node.set_enabled(enabled)
        except Exception as err:  # pylint: disable=broad-except
            verb = "enable" if enabled else "disable"
            raise HomeAssistantError(
                f"Unable to {verb} device {self._node.address}: {err}"
            ) from err
        self.async_write_ha_state()


class ISYAuxControlSwitchEntity(ISYNodeEntity, RestoreEntity, SwitchEntity):
    """A coalesced boolean aux control as a switch (Insteon i3
    ``*Flags`` GVx, plugin bool setters).

    Readback if the control is a nodedef property, else optimistic
    (restored across restarts via :class:`RestoreEntity`).
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the aux-control switch entity."""
        super().__init__(*args, **kwargs)
        self._attr_entity_category = aux_entity_category(self._control)
        self._optimistic_value: bool | None = None

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
        """Restore the last set value for a write-only control."""
        await super().async_added_to_hass()
        if not self._has_readback and (last := await self.async_get_last_state()):
            self._optimistic_value = last.state == STATE_ON

    @property
    def is_on(self) -> bool | None:
        """Return whether the control is currently on."""
        if not self._has_readback:
            return self._optimistic_value

        node_prop: NodePropertyValue | None = self._node.properties.get(self._control)
        if node_prop is None or not node_prop.value:
            return None
        # pyisyox normalised the value to the control's editor unit; a
        # binary editor lands on 0 / non-zero (1, 100, 255 — all "on").
        try:
            return bool(int(float(node_prop.value)))
        except (TypeError, ValueError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the control's "on" value."""
        await self._async_send(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the control's "off" value."""
        await self._async_send(False)

    async def _async_send(self, turn_on: bool) -> None:
        """Send ``control`` with ``1`` / ``0`` (codec-encoded on the wire)."""
        try:
            await self._node.send_command(self._control, int(turn_on))
        except NodeCommandError as err:
            verb = "on" if turn_on else "off"
            raise HomeAssistantError(
                f"Could not turn {verb} {self.name} for {self._node.address}: {err}"
            ) from err
        if not self._has_readback:
            self._optimistic_value = turn_on
            self.async_write_ha_state()


class ISYProgramEnableSwitch(ISYProgramDeviceEntity, SwitchEntity):
    """Enable / disable the program on the controller."""

    _attr_translation_key = "program_enable"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:script-text-outline"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_ENABLE_SWITCH_SUFFIX
        )

    @property
    def is_on(self) -> bool | None:
        """``True`` when the program is enabled on the controller."""
        return self._node.enabled

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable the program."""
        try:
            await self._node.enable()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to enable program {self._node.address}: {err}"
            ) from err

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable the program."""
        try:
            await self._node.disable()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to disable program {self._node.address}: {err}"
            ) from err


class ISYProgramRunAtStartupSwitch(ISYProgramDeviceEntity, SwitchEntity):
    """Toggle the program's "run at startup" flag."""

    _attr_translation_key = "program_run_at_startup"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:restart"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_RUN_AT_STARTUP_SWITCH_SUFFIX
        )

    @property
    def is_on(self) -> bool | None:
        """``True`` when the program is set to auto-run on boot."""
        return self._node.run_at_startup

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Set the run-at-startup flag."""
        try:
            await self._node.enable_run_at_startup()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to enable run-at-startup for {self._node.address}: {err}"
            ) from err

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Clear the run-at-startup flag."""
        try:
            await self._node.disable_run_at_startup()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to disable run-at-startup for {self._node.address}: {err}"
            ) from err
