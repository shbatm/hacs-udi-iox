"""Representation of IoX buttons."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyisyox import (
    Controller,
    NetworkResource,
    Node,
    NodeCommandError,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    Program,
)
from pyisyox.constants import TAG_ENABLED, Protocol

from .const import CONF_NETWORK, DOMAIN
from .models import IsyConfigEntry, IsyData
from .program_device import (
    PROGRAM_RUN_BUTTON_SUFFIX,
    PROGRAM_RUN_ELSE_BUTTON_SUFFIX,
    PROGRAM_RUN_IF_BUTTON_SUFFIX,
    PROGRAM_RUN_THEN_BUTTON_SUFFIX,
    PROGRAM_STOP_BUTTON_SUFFIX,
    ISYProgramDeviceEntity,
)

#: Plugin-defined accept commands whose semantics are "make the device
#: announce itself" — tag the button with ``ButtonDeviceClass.IDENTIFY``
#: so HA renders the identify affordance. ``BEEP`` is Insteon's; plugins
#: occasionally reuse the verb.
_IDENTIFY_COMMANDS = frozenset({"BEEP"})

#: Accept-command buttons created disabled by default — verbs that
#: most users never press from HA, kept discoverable in the entity
#: registry for those who want them:
#:  * ``WDU`` "Write Changes" — commits queued config to an Insteon
#:    device's EEPROM.
#:  * ``DFON`` / ``DFOF`` "Fast On" / "Fast Off" and the momentary
#:    paddle-simulation verbs ``BRT`` / ``DIM`` (brighten / dim) and
#:    ``FDUP`` / ``FDDOWN`` / ``FDSTOP`` (fade up / down / stop) —
#:    pyisyox's classifier now leaves these out of the light platform
#:    (it only claims ``DON`` / ``DOF``) so they reach us as buttons;
#:    they're niche, so don't clutter the device page by default.
_DISABLED_BY_DEFAULT_COMMANDS = frozenset(
    {"WDU", "DFON", "DFOF", "BRT", "DIM", "FDUP", "FDDOWN", "FDSTOP"}
)


def _command_label(node: Node, command_id: str) -> str:
    """Friendly label for a plugin accept command, from the nodedef."""
    nodedef = node.nodedef
    if nodedef is not None:
        for cmd in nodedef.cmds.accepts:
            if cmd.id == command_id:
                if cmd.name:
                    return cmd.name
                break
    return command_id.replace("_", " ").title()


PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: IsyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IoX buttons from a config entry."""
    isy_data = config_entry.runtime_data
    controller: Controller = isy_data.root
    device_info = isy_data.devices
    entities: list[
        ISYNodeQueryButtonEntity
        | ISYNodeBeepButtonEntity
        | ISYNodeCommandButtonEntity
        | ISYNetworkResourceButtonEntity
    ] = []

    for node in isy_data.root_nodes[Platform.BUTTON]:
        entities.append(
            ISYNodeQueryButtonEntity(
                isy_data,
                node=node,
                name="Query",
                unique_id=f"{isy_data.uid_base(node)}_query",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_info=device_info[node.address],
            )
        )
        if node.protocol == Protocol.INSTEON:
            entities.append(
                ISYNodeBeepButtonEntity(
                    isy_data,
                    node=node,
                    name="Beep",
                    unique_id=f"{isy_data.uid_base(node)}_beep",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    device_info=device_info[node.address],
                )
            )

    # Plugin-defined zero-arg accept commands → one button each. pyisyox's
    # classifier has already excluded QUERY and controllable-claimed cmds;
    # commands with a required parameter live in result.parameterized_commands
    # and aren't surfaced here.
    for node, command_id in isy_data.aux_properties[Platform.BUTTON]:
        entities.append(
            ISYNodeCommandButtonEntity(
                isy_data,
                node=node,
                command_id=command_id,
                name=_command_label(node, command_id),
                unique_id=f"{isy_data.uid_base(node)}_{command_id}",
                device_info=device_info[node.address],
            )
        )

    for resource in isy_data.net_resources:
        entities.append(
            ISYNetworkResourceButtonEntity(
                isy_data,
                node=resource,
                name=resource.name,
                unique_id=isy_data.uid_base(resource),
                device_info=device_info[CONF_NETWORK],
            )
        )

    # System-wide query button
    entities.append(
        ISYNodeQueryButtonEntity(
            isy_data,
            node=controller,
            name="Query",
            unique_id=f"{controller.config.uuid}_query",
            device_info=DeviceInfo(identifiers={(DOMAIN, controller.config.uuid)}),
            entity_category=EntityCategory.DIAGNOSTIC,
        )
    )

    program_buttons: list[
        ISYProgramRunButton
        | ISYProgramRunThenButton
        | ISYProgramRunElseButton
        | ISYProgramRunIfButton
        | ISYProgramStopButton
    ] = []
    for program in isy_data.program_devices:
        program_dev = device_info.get(f"program_{program.address}")
        if program_dev is None:
            continue
        program_buttons.append(ISYProgramRunButton(isy_data, program, program_dev))
        program_buttons.append(ISYProgramRunThenButton(isy_data, program, program_dev))
        program_buttons.append(ISYProgramRunElseButton(isy_data, program, program_dev))
        program_buttons.append(ISYProgramRunIfButton(isy_data, program, program_dev))
        program_buttons.append(ISYProgramStopButton(isy_data, program, program_dev))

    async_add_entities([*entities, *program_buttons])


class ISYNodeButtonEntity(ButtonEntity):
    """Base for IoX device-button entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _node: Node | Controller | NetworkResource

    def __init__(
        self,
        isy_data: IsyData,
        node: Node | Controller | NetworkResource,
        name: str,
        unique_id: str,
        device_info: DeviceInfo,
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize a button entity."""
        self._isy_data = isy_data
        self._node = node

        self._attr_name = name
        self._attr_entity_category = entity_category
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        # NetworkResource and Controller don't carry an enabled flag;
        # default to True so the button is always usable.
        self._node_enabled = getattr(node, TAG_ENABLED, True)
        self._unsubscribers: list[Callable[[], None]] = []

    @property
    def available(self) -> bool:
        """Return entity availability.

        Combines node-enabled (for node-backed buttons) with WS health —
        a dropped event stream marks every button unavailable so the
        user doesn't fire a command at a controller we can't observe.
        Silver ``entity-unavailable`` rule.
        """
        if not self._isy_data.controller_events.ws_connected:
            return False
        return self._node_enabled

    async def async_added_to_hass(self) -> None:
        """Subscribe to lifecycle + WS-status events for availability tracking."""
        events = self._isy_data.controller_events
        self._unsubscribers.append(events.subscribe_ws_status(self._on_ws_status))
        if not isinstance(self._node, Node):
            # NetworkResource and system-query buttons aren't nodes.
            return
        self._unsubscribers.append(events.subscribe_lifecycle(self._on_lifecycle))

    @callback
    def _on_ws_status(self, connected: bool) -> None:
        """Refresh state on WS flip so ``available`` re-renders."""
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from controller events."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Update availability when the controller toggles the node."""
        if event.node_address != self._node.address:
            return
        if event.action != NodeLifecycleAction.NODE_ENABLED:
            return
        self._node_enabled = getattr(self._node, TAG_ENABLED, True)
        self.async_write_ha_state()


class ISYNodeQueryButtonEntity(ISYNodeButtonEntity):
    """Press → :meth:`Node.send_command` ``QUERY`` (or ``Controller.refresh``)."""

    _node: Node | Controller

    async def async_press(self) -> None:
        """Press the button."""
        if isinstance(self._node, Controller):
            target = self._node.config.uuid
            try:
                await self._node.refresh()
            except Exception as err:  # pylint: disable=broad-except
                raise HomeAssistantError(
                    f"Unable to refresh controller {target}: {err}"
                ) from err
        else:
            target = self._node.address
            try:
                await self._node.send_command("QUERY")
            except NodeCommandError as err:
                raise HomeAssistantError(
                    f"Unable to query node {target}: {err}"
                ) from err


class ISYNodeBeepButtonEntity(ISYNodeButtonEntity):
    """Press → Insteon beep (zero-arg; the controller applies a default
    level). Tagged ``identify`` so HA renders the identify affordance."""

    _node: Node
    _attr_device_class = ButtonDeviceClass.IDENTIFY

    async def async_press(self) -> None:
        """Press the button."""
        try:
            await self._node.send_command("BEEP")
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to beep node {self._node.address}: {err}"
            ) from err


class ISYNodeCommandButtonEntity(ISYNodeButtonEntity):
    """Press → send a plugin-defined zero-arg accept command to the node.

    Covers parameterless verbs (DISCOVER, SETFAILED, ...) and ones whose
    parameters are all optional (controller applies defaults). Callers who
    need a non-default parameter value use the ``send_node_command``
    service instead.

    Categorised ``config`` — these are device-configuration verbs
    (re-discover, reset, identify, ...) the user invokes deliberately,
    not primary controls and not read-only diagnostics.
    """

    _node: Node

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        *,
        command_id: str,
        name: str,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        """Bind to a single accept command on the node."""
        super().__init__(
            isy_data,
            node,
            name=name,
            unique_id=unique_id,
            device_info=device_info,
            entity_category=EntityCategory.CONFIG,
        )
        self._command_id = command_id
        if command_id in _IDENTIFY_COMMANDS:
            self._attr_device_class = ButtonDeviceClass.IDENTIFY
        if command_id in _DISABLED_BY_DEFAULT_COMMANDS:
            self._attr_entity_registry_enabled_default = False

    async def async_press(self) -> None:
        """Press the button — send the verb with no arguments."""
        try:
            await self._node.send_command(self._command_id)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to send {self._command_id} to {self._node.address}: {err}"
            ) from err


class ISYNetworkResourceButtonEntity(ISYNodeButtonEntity):
    """Press → run an IoX network resource."""

    _attr_has_entity_name = False
    _node: NetworkResource

    async def async_press(self) -> None:
        """Fire the network resource (HTTP / TCP / UDP trigger
        configured on the controller)."""
        try:
            await self._node.run()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to run network resource {self._node.name}: {err}"
            ) from err


class _ISYProgramButtonBase(ISYProgramDeviceEntity, ButtonEntity):
    """Shared scaffolding for the per-program-device manual run buttons.

    Each subclass binds one verb on :class:`pyisyox.Program` (``run``,
    ``run_then``, ``run_else``, ``run_if``, ``stop``) and translates
    failures to :class:`HomeAssistantError`. The ``_verb`` /
    ``_verb_label`` ClassVars are declared without defaults so a
    forgotten subclass override surfaces immediately at runtime
    instead of silently dispatching to ``getattr(node, "")``.
    """

    _verb: ClassVar[str]
    _verb_label: ClassVar[str]

    async def async_press(self) -> None:
        """Invoke the bound program verb."""
        method: Callable | None = getattr(self._node, self._verb, None)
        if method is None:
            raise HomeAssistantError(
                f"Program {self._node.address} has no verb {self._verb!r}"
            )
        try:
            await method()
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(
                f"Unable to {self._verb_label} program {self._node.address}: {err}"
            ) from err


class ISYProgramRunButton(_ISYProgramButtonBase):
    """Run the program (controller invokes whichever clause its ``if`` decides)."""

    _verb = "run"
    _verb_label = "run"
    _attr_translation_key = "program_run"
    _attr_icon = "mdi:play"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_RUN_BUTTON_SUFFIX
        )


class ISYProgramRunThenButton(_ISYProgramButtonBase):
    """Force the program's ``then`` clause."""

    _verb = "run_then"
    _verb_label = "run then-clause of"
    _attr_translation_key = "program_run_then"
    _attr_icon = "mdi:play-circle"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_RUN_THEN_BUTTON_SUFFIX
        )


class ISYProgramRunElseButton(_ISYProgramButtonBase):
    """Force the program's ``else`` clause."""

    _verb = "run_else"
    _verb_label = "run else-clause of"
    _attr_translation_key = "program_run_else"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_RUN_ELSE_BUTTON_SUFFIX
        )


class ISYProgramRunIfButton(_ISYProgramButtonBase):
    """Re-evaluate the program's ``if`` condition without running clauses."""

    _verb = "run_if"
    _verb_label = "re-evaluate"
    _attr_translation_key = "program_run_if"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:refresh"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_RUN_IF_BUTTON_SUFFIX
        )


class ISYProgramStopButton(_ISYProgramButtonBase):
    """Stop a currently running program."""

    _verb = "stop"
    _verb_label = "stop"
    _attr_translation_key = "program_stop"
    _attr_icon = "mdi:stop"

    def __init__(
        self, isy_data: IsyData, program: Program, device_info: DeviceInfo
    ) -> None:
        super().__init__(
            isy_data, program, device_info, suffix=PROGRAM_STOP_BUTTON_SUFFIX
        )
