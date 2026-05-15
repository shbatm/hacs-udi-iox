"""Representation of IoX entity types."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription
from pyisyox import (
    Event,
    Folder,
    Group,
    Node,
    NodeCommandError,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    NodePropertyValue,
    Program,
)
from pyisyox.constants import (
    COMMAND_FRIENDLY_NAME,
    ISY_VALUE_UNKNOWN,
    PROP_STATUS,
)
from pyisyox.schema.editor import EditorRange
from pyisyox.schema.nodedef import NodeDef

from .const import DOMAIN
from .editor_classification import range_for_control

if TYPE_CHECKING:
    from pyisyox import Variable

    from .models import IsyData

# Lazy aliases — keep the import graph acyclic.
type NodeType = Node | Group | Folder | Program | Variable
type NodeEventType = NodePropertyValue | NodeLifecycleEvent


def _resolve_device_info(
    devices: dict[str, DeviceInfo], node: Node
) -> DeviceInfo | None:
    """Return the DeviceInfo this entity should attach to.

    1. ``node.address`` — nodes with their own DeviceInfo (roots +
       node-server plugin children).
    2. ``node.primary_address`` — sub-nodes inherit the primary's.
    """
    info = devices.get(node.address)
    if info is not None:
        return info
    if node.primary_address is not None:
        return devices.get(node.primary_address)
    return None


def node_status_int(node: Node) -> int | None:
    """Read ``node.status`` as a scalar int (or ``None`` when unknown)."""
    prop = node.status
    if prop is None or not prop.value or prop.value == ISY_VALUE_UNKNOWN:
        return None
    try:
        return int(float(prop.value))
    except (ValueError, TypeError):
        return None


def _strip_parent_prefix(name: str, parent_name: str | None) -> str:
    """Strip a parent device's name from the front of a sub-node label.

    ``"Hallway Keypad B"`` under device ``"Hallway Keypad"`` → ``"B"``;
    HA prepends the device name via ``has_entity_name``, so leaving the
    full prefix doubles it (``"Hallway Keypad Hallway Keypad B"``).
    """
    if parent_name and name.startswith(parent_name):
        return name[len(parent_name) :].lstrip(" -_:.") or name
    return name


class ISYEntity(Entity):
    """Base class for IoX entities backed by a runtime Node."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _node: NodeType
    _isy_data: IsyData
    _unsubscribers: list[Callable[[], None]]

    def __init__(
        self,
        isy_data: IsyData,
        node: NodeType,
        device_info: DeviceInfo | None = None,
        unique_id: str | None = None,
    ) -> None:
        """Initialize the entity. ``unique_id`` defaults to ``{uuid}_{address}``."""
        self._isy_data = isy_data
        self._node = node
        self._attr_name = getattr(node, "name", "") or ""
        uuid = isy_data.uuid
        address = self._node_address()
        if device_info is None:
            device_info = DeviceInfo(identifiers={(DOMAIN, uuid)})
        self._attr_device_info = device_info
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{uuid}_{address}"
        )
        self._attrs: dict[str, Any] = {}
        self._unsubscribers: list[Callable[[], None]] = []

    def _node_address(self) -> str:
        """Address used for unique-id + event dispatch.

        Returns the node's wire address for runtime nodes; falls back
        to the dict key for raw program/variable records.
        """
        node = self._node
        if isinstance(node, dict):
            return str(node.get("address", node.get("id", "")))
        return node.address

    async def async_added_to_hass(self) -> None:
        """Subscribe to events for this node via the central registry."""
        events = self._isy_data.controller_events
        self._unsubscribers.append(
            events.subscribe_node(self._node_address(), None, self._on_node_event)
        )
        self._unsubscribers.append(events.subscribe_ws_status(self._on_ws_status))

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from node events."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    @property
    def available(self) -> bool:
        """``_attr_available`` AND WS connected."""
        if not self._isy_data.controller_events.ws_connected:
            return False
        return self._attr_available

    @callback
    def _on_node_event(self, event: Event) -> None:
        """Default handler — dispatch to ``async_on_update``."""
        self.async_on_update(event, self.unique_id or "")

    @callback
    def _on_ws_status(self, connected: bool) -> None:
        """Rerender so the dynamic ``available`` picks up the new flag."""
        self.async_write_ha_state()

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle the update event from the controller."""
        self.async_write_ha_state()


class ISYGroupEntity(ISYEntity):
    """Representation of a ISY Group entity."""

    # Scenes carry full descriptive names from the controller ("Living
    # Room Scene"). Opt out of has_entity_name so the friendly name
    # stays the scene name as-is, regardless of which device the entity
    # attaches to (controller hub for multi-controller scenes, or a
    # specific node device for single-controller scenes).
    _attr_has_entity_name = False
    _node: Group

    @property
    def extra_state_attributes(self) -> dict:
        """Get the state attributes for the device."""
        return {"group_all_on": self._node.group_all_on}

    async def async_rename_node(self, name: str) -> None:
        """Rename the underlying group on the controller.

        The IoX rename endpoint takes the same path for nodes and
        groups; ``Group.rename`` posts ``nodeType: "group"`` so the
        server dispatches through the scene registry.
        """
        await self._node.rename(name)


class ISYNodeEntity(ISYEntity):
    """Base class for IoX entities scoped to a single Node + control."""

    _node: Node
    _control: str
    _node_def: NodeDef | None = None

    def __init__(
        self,
        isy_data: IsyData,
        node: Node,
        control: str = PROP_STATUS,
        unique_id: str | None = None,
        description: EntityDescription | None = None,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the IoX node entity."""
        super().__init__(isy_data, node, device_info=device_info, unique_id=unique_id)
        self._control = control
        if description is not None:
            self.entity_description = description

        # Name composition (has_entity_name=True; ``_attr_name`` is the
        # suffix or ``None`` to use the device name as-is):
        # - Primary on a node owning its DeviceInfo → ``None``.
        # - Primary on a folded sub-node → sub-name with parent prefix
        #   stripped (avoids duplicating the device prefix).
        # - Aux → property label or COMMAND_FRIENDLY_NAME, prefixed with
        #   the sub-name on folded sub-nodes so e.g. a sub-node "Ramp
        #   Rate" doesn't render identically to the parent's.
        self._node_def = node.nodedef
        node_owns_device = (
            node.primary_address is None or node.protocol == "node_server"
        )
        if control == PROP_STATUS:
            if node_owns_device:
                name: str | None = None
            else:
                parent = isy_data.root.nodes.get(node.primary_address)
                parent_name = parent.name if parent is not None else None
                name = _strip_parent_prefix(node.name, parent_name)
        else:
            label: str | None = None
            if self._node_def is not None and (
                prop := self._node_def.properties.get(control)
            ):
                label = prop.name or None
            if label is None:
                label = (
                    COMMAND_FRIENDLY_NAME.get(control, control)
                    .replace("_", " ")
                    .title()
                )
            if node_owns_device:
                name = label
            else:
                parent = isy_data.root.nodes.get(node.primary_address)
                parent_name = parent.name if parent is not None else None
                prefix = _strip_parent_prefix(node.name, parent_name)
                name = f"{prefix} {label}".strip() if prefix else label

        self._attr_name = name
        self._attr_available = node.enabled

    async def async_added_to_hass(self) -> None:
        """Subscribe to control + lifecycle + WS status."""
        events = self._isy_data.controller_events
        self._unsubscribers.append(
            events.subscribe_node(
                self._node.address, self._control, self._on_node_event
            )
        )
        self._unsubscribers.append(events.subscribe_lifecycle(self._on_lifecycle))
        self._unsubscribers.append(events.subscribe_ws_status(self._on_ws_status))

    @callback
    def _on_lifecycle(self, event: NodeLifecycleEvent) -> None:
        """Update entity availability when the controller toggles the node."""
        if event.node_address != self._node.address:
            return
        if event.action != NodeLifecycleAction.NODE_ENABLED:
            return
        self._attr_available = self._node.enabled
        self.async_write_ha_state()

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event from the controller."""
        self._attr_available = self._node.enabled
        self.async_write_ha_state()

    def _accepted_command_ids(self) -> set[str] | None:
        """Wire ids the node's nodedef declares it accepts.

        Returns ``None`` when the nodedef isn't resolved — callers
        should treat that as "can't validate, let it through" rather
        than "accepts nothing".
        """
        nodedef = self._node.nodedef
        if nodedef is None:
            return None
        return {cmd.id for cmd in nodedef.cmds.accepts}

    def _validate_command(self, command_id: str) -> None:
        """Raise if ``command_id`` isn't in the node's accept set; the
        error surfaces the valid list."""
        accepted = self._accepted_command_ids()
        if accepted is not None and command_id not in accepted:
            raise ServiceValidationError(
                f"Node {self._node.address} does not accept command "
                f"{command_id!r}. Accepted commands: {', '.join(sorted(accepted))}"
            )

    async def async_get_node_commands(self) -> dict[str, dict[str, str]]:
        """Return ``{"accepted_commands": {id: friendly_name}}``,
        sorted by wire id; empty when the nodedef isn't resolved."""
        nodedef = self._node.nodedef
        if nodedef is None:
            return {"accepted_commands": {}}
        return {
            "accepted_commands": {
                cmd.id: cmd.name or cmd.id
                for cmd in sorted(nodedef.cmds.accepts, key=lambda c: c.id)
            }
        }

    async def async_send_node_command(self, command: str) -> None:
        """Translate friendly name → IoX command id, validate, send."""
        # Reverse the friendly-name → IoX-id map.
        friendly_to_id = {v: k for k, v in COMMAND_FRIENDLY_NAME.items()}
        cmd_id = friendly_to_id.get(command, command)
        self._validate_command(cmd_id)
        try:
            await self._node.send_command(cmd_id)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to send {cmd_id} to {self._node.address}: {err}"
            ) from err

    async def async_send_raw_node_command(
        self,
        command: str,
        value: Any | None = None,
        unit_of_measurement: str | None = None,
        parameters: Any | None = None,
    ) -> None:
        """Send a raw ``(command, value)`` pair; ``unit_of_measurement``
        and ``parameters`` are ignored — the editor codec resolves them."""
        self._validate_command(command)
        params = (value,) if value is not None else ()
        try:
            await self._node.send_command(command, *params)
        except NodeCommandError as err:
            raise HomeAssistantError(
                f"Unable to send {command} to {self._node.address}: {err}"
            ) from err

    async def async_get_zwave_parameter(self, parameter: int) -> dict[str, int]:
        """Read a Z-Wave parameter.

        Returns the ``{"parameter", "size", "value"}`` dict shape PyISY
        3.x used so isy994-migrated automations keep the same keys.
        """
        try:
            return await self._node.get_zwave_parameter(parameter)
        except NodeCommandError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_zwave_parameter(
        self, parameter: int, value: int, size: int
    ) -> None:
        """Write a Z-Wave parameter via the dedicated wire path that
        carries ``size`` — the legacy CONFIG cmd can't model byte width,
        which is why CONFIG auto-fan-out is suppressed on Z-Wave."""
        try:
            await self._node.set_zwave_parameter(parameter, value, size)
        except NodeCommandError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_rename_node(self, name: str) -> None:
        """Rename the node; controller emits ``NN`` lifecycle which
        triggers the reload-required Repair card."""
        await self._node.rename(name)

    def _editor_range_for(self, control: str) -> EditorRange | None:
        """Write-side editor range for ``control``; ``None`` if unresolved.

        UOM keys callers care about:
        * ``UOM_PERCENTAGE`` (51) → 0-100 percent.
        * ``UOM_8_BIT_RANGE`` (100) → raw byte; check ``max`` for the
          0-255 vs 0-100 SwitchLinc/KeypadDimmer_ADV distinction.
        * ``names`` non-empty → enum (routed to SELECT).
        """
        return range_for_control(self._isy_data.root, self._node, control)


class ISYProgramEntity(ISYEntity):
    """Program-based entity. Subscribes via ``subscribe_program`` (the
    dedicated channel for ``_1`` action ``"0"`` frames) rather than the
    per-(addr, control) registry nodes use."""

    # Opt out of has_entity_name so HA uses the program name verbatim
    # (else it'd prepend the controller name → "eisy.local Movie Mode").
    _attr_has_entity_name = False
    _node: Program
    _actions: Program | None

    def __init__(
        self,
        isy_data: IsyData,
        name: str,
        status: Program,
        actions: Program | None = None,
    ) -> None:
        """Initialize the program-based entity. ``status`` is the
        program this entity reflects (becomes ``self._node``);
        ``actions`` is the optional sibling program that runs the
        ``then`` / ``else`` clauses for non-binary platforms."""
        super().__init__(isy_data, status)
        self._attr_name = name
        self._actions = actions

    async def async_added_to_hass(self) -> None:
        """Subscribe to status updates for the underlying program.

        The base ``ISYEntity.async_added_to_hass`` subscribes through
        the per-(addr, control) registry which doesn't dispatch
        program-status frames; programs need the dedicated
        ``subscribe_program`` channel.
        """
        program: Program = self._node  # type: ignore[assignment]
        events = self._isy_data.controller_events
        self._unsubscribers.append(
            events.subscribe_program(program.address, self._on_program_status)
        )
        self._unsubscribers.append(events.subscribe_ws_status(self._on_ws_status))

    @callback
    def _on_program_status(self, event: object) -> None:
        """Refresh HA state when the program toggles.

        ``ProgramRecord.status`` has already been mutated by the
        pyisyox dispatcher, so ``is_on`` / ``is_locked`` / ``is_closed``
        / etc. read the new value on the very next render.
        """
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        """Surfaces actions / status program metadata pyisy 3.x
        consumers expected; timestamps stay as ISO 8601 strings."""
        attr: dict[str, Any] = {}
        actions = self._actions
        if actions is not None:
            attr["actions_enabled"] = actions.enabled
            attr["actions_last_finish"] = actions.last_finish_time
            attr["actions_last_run"] = actions.last_run_time
            attr["actions_next_scheduled_run"] = actions.next_scheduled_run_time
            attr["run_at_startup"] = actions.run_at_startup
            attr["running"] = actions.running

        status = self._node
        if isinstance(status, Program):
            attr["status_enabled"] = status.enabled
            attr["status_last_finish"] = status.last_finish_time
            attr["status_last_run"] = status.last_run_time
            attr["status_next_scheduled_run"] = status.next_scheduled_run_time
        return attr
