"""Support for ISY switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import Group, Node, NodeCommandError, Program
from pyisyox.constants import CMD_OFF, CMD_ON

from .entity import (
    ISYGroupEntity,
    ISYNodeEntity,
    ISYProgramEntity,
    NodeEventType,
    _resolve_device_info,
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
        # Currently only used for enable switches, will need to be updated for
        # NS support by making sure control == TAG_ENABLED
        description = ISYSwitchEntityDescription(
            key=control,
            device_class=SwitchDeviceClass.SWITCH,
            name=control.title(),
            entity_category=EntityCategory.CONFIG,
        )
        entities.append(
            ISYEnableSwitchEntity(
                isy_data,
                node=node,
                control=control,
                unique_id=f"{isy_data.uid_base(node)}_{control}",
                description=description,
                device_info=_resolve_device_info(device_info, node),
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
    """Representation of an ISY group switch device.

    ``pyisyox.Group`` deliberately exposes no live ``status`` — groups
    don't carry wire-level state of their own. The is_on aggregation is
    derived on access from the controller's nodes registry via
    ``group_any_on`` (any member currently non-zero).
    """

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
    """A representation of an ISY program switch.

    ``status`` (``self._node``) is the program that drives ``is_on``.
    ``self._actions`` is the sibling program that runs on user input
    — its ``then`` clause turns the switch on; ``else`` turns it off.
    The status program flips back through the WS dispatcher; the
    optimistic local state is the ``then`` / ``else`` branch we just
    requested.
    """

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
        events = getattr(self._isy_data, "controller_events", None)
        return True if events is None else events.ws_connected

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Reflect a control / lifecycle event in the switch state."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Whether the node is enabled on the controller.

        ``node.enabled`` tracks ``EN`` lifecycle frames (pyisyox writes
        the new state back to the record), so this follows changes made
        from the admin console / REST as well as from this switch.
        """
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
