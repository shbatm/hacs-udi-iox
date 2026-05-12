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

if TYPE_CHECKING:
    from pyisyox import Variable

    from .models import IsyData

# PEP 695 lazy type aliases — the right-hand side is evaluated only when
# the alias is consumed, so the (TYPE_CHECKING-only) Program / Variable
# references don't pull models.py at import time. Keeps the import graph
# acyclic.
type NodeType = Node | Group | Folder | Program | Variable
type NodeEventType = NodePropertyValue | NodeLifecycleEvent


def _resolve_device_info(
    devices: dict[str, DeviceInfo], node: Node
) -> DeviceInfo | None:
    """Return the DeviceInfo this entity should attach to.

    Lookup order:

    1. ``node.address`` — every node that has its own DeviceInfo (root
       nodes AND node-server plugin children, per ``_has_own_device``)
       is keyed here. Plugin children are kept as separate HA devices
       to match the eisy UI's per-sensor cards instead of folding
       every Flume sensor's aux into one shared device.
    2. ``node.primary_address`` — native sub-nodes (KeypadLinc sub-
       buttons, FanLinc fan-vs-light sides) fall through to their
       physical primary's DeviceInfo.
    """
    info = devices.get(node.address)
    if info is not None:
        return info
    if node.primary_address is not None:
        return devices.get(node.primary_address)
    return None


def node_status_int(node: Node) -> int | None:
    """Read ``node.status`` as a scalar int (or ``None`` when unknown).

    pyisyox 6 exposes ``Node.status`` as a structured
    :class:`NodePropertyValue` so callers can also reach uom/formatted.
    HA platforms only ever want the numeric value — this helper does
    the unwrap and coerces the string-encoded value to int. Callers
    that want a float, the formatted string, or the uom should still
    reach for ``node.status.value`` / ``.formatted`` / ``.uom`` directly.
    """
    prop = node.status
    if prop is None or not prop.value or prop.value == ISY_VALUE_UNKNOWN:
        return None
    try:
        return int(float(prop.value))
    except (ValueError, TypeError):
        return None


def _strip_parent_prefix(name: str, parent_name: str | None) -> str:
    """Strip a parent device's name from the front of a sub-node label.

    ISY users commonly label sub-nodes as ``"<device> <suffix>"`` (e.g.
    ``"Hallway Keypad B"`` under ``"Hallway Keypad"``). With
    ``has_entity_name=True`` Home Assistant prepends the device name
    when rendering the friendly name, so the entity name itself only
    needs to carry the suffix — otherwise the device name appears
    twice (``"Hallway Keypad Hallway Keypad B"``). When the sub-node
    name doesn't start with the parent's name, returns it unchanged so
    the user's intent is preserved.
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
        """Initialize the entity.

        Args:
            isy_data: Runtime data carrier — used to reach the central
                event-dispatch registry on
                :class:`controller_events.IsyControllerEvents` and the
                controller uuid for unique-id construction.
            node: The runtime Node / Group / Folder / record this entity
                wraps.
            device_info: Optional pre-built DeviceInfo. Defaults to a
                stub identified by the controller uuid.
            unique_id: Override for the entity's unique_id. Defaults to
                ``f"{controller.uuid}_{node.address}"``.
        """
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
        self._unsubscribers.append(
            self._isy_data.controller_events.subscribe_node(
                self._node_address(), None, self._on_node_event
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from node events."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    @callback
    def _on_node_event(self, event: Event) -> None:
        """Default handler — write HA state on every property update.

        Subclasses that need finer-grained behavior override
        :meth:`async_on_update` (which still receives the legacy
        ``(event, key)`` signature for backward compatibility) or
        override this method directly.
        """
        self.async_on_update(event, self.unique_id or "")

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

        # Name composition (has_entity_name=True throughout — HA prepends
        # the device name automatically; ``_attr_name`` carries the entity's
        # own suffix only, or ``None`` to mean "use the device name as-is").
        #
        # - Primary entity (PROP_STATUS) on a node that owns its DeviceInfo
        #   (top-level root OR node-server plugin child) → ``None`` so HA
        #   renders the device name as-is.
        # - Primary entity on a native sub-node folded under a parent
        #   device (FanLinc motor side, KeypadLinc sub-buttons) → the
        #   sub-node's name with the parent's prefix stripped, so the
        #   rendered friendly name doesn't duplicate the device prefix.
        # - Aux control entity → the property's nodedef label, falling
        #   back to the IoX command friendly-name table. For node-server
        #   children this lives on the child's own device so the label
        #   doesn't collide with sibling sensors.
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
            name = label

        self._attr_name = name
        self._attr_available = node.enabled

    async def async_added_to_hass(self) -> None:
        """Subscribe to property changes for this node's control + the
        node-enabled lifecycle so we can flip ``available`` when the
        controller (de)activates the device."""
        events = self._isy_data.controller_events
        self._unsubscribers.append(
            events.subscribe_node(
                self._node.address, self._control, self._on_node_event
            )
        )
        self._unsubscribers.append(events.subscribe_lifecycle(self._on_lifecycle))

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
        """Raise if ``command_id`` isn't in the node's accept set.

        Just-in-time against the live nodedef — saves a round-trip to
        the controller for a verb it would reject, and gives the caller
        the valid list instead of an opaque protocol error.
        """
        accepted = self._accepted_command_ids()
        if accepted is not None and command_id not in accepted:
            raise ServiceValidationError(
                f"Node {self._node.address} does not accept command "
                f"{command_id!r}. Accepted commands: {', '.join(sorted(accepted))}"
            )

    async def async_get_node_commands(self) -> dict[str, dict[str, str]]:
        """Entity-service response: the node's accepted-command vocabulary.

        ``accepted_commands`` is an id→friendly-name mapping (the wire
        ids — ``DON``, ``OL``, ``BEEP`` — are what ``send_raw_node_command``
        expects; the values are the nodedef ``name`` strings for display).
        Sorted by wire id; empty when the nodedef isn't resolved.
        """
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
        """Respond to an entity service command call.

        The legacy v3 ``send_node_command`` service mapped friendly
        names ("brighten", "fast_off") onto Node helper methods. v6
        unifies on Node.send_command — translate friendly names to the
        canonical IoX command id, validate against the node's accept
        set, then let the editor codec validate the parameters.
        """
        # Reverse the friendly-name → IoX-id map.
        friendly_to_id = {v: k for k, v in COMMAND_FRIENDLY_NAME.items()}
        cmd_id = friendly_to_id.get(command, command)
        self._validate_command(cmd_id)
        await self._node.send_command(cmd_id)

    async def async_send_raw_node_command(
        self,
        command: str,
        value: Any | None = None,
        unit_of_measurement: str | None = None,
        parameters: Any | None = None,
    ) -> None:
        """Respond to an entity-service raw command call.

        ``unit_of_measurement`` and ``parameters`` are ignored — the
        editor codec on :meth:`Node.send_command` resolves them from
        the node's profile. Only ``(command, value)`` is honored.
        """
        self._validate_command(command)
        params = (value,) if value is not None else ()
        await self._node.send_command(command, *params)

    async def async_get_zwave_parameter(self, parameter: int) -> None:
        """Z-Wave parameter read — not supported."""
        raise HomeAssistantError("Z-Wave parameter services are not supported")

    async def async_set_zwave_parameter(
        self, parameter: int, value: int, size: int
    ) -> None:
        """Z-Wave parameter write — not supported."""
        raise HomeAssistantError("Z-Wave parameter services are not supported")

    async def async_rename_node(self, name: str) -> None:
        """Rename the underlying node on the controller.

        The IoX server emits a ``NodeLifecycleEvent`` with action
        ``NN`` after the rename succeeds; the lifecycle Repair card
        prompts the user to reload the entry so HA's name caches
        catch up.
        """
        await self._node.rename(name)

    def _editor_range_for(self, control: str) -> EditorRange | None:
        """Return the write-side editor range for ``control`` on this node.

        Editor resolution is determined by the **control's** ``editor_id``
        (looked up via the property on this node's nodedef, then resolved
        against the profile scoped to ``(family_id, instance_id)``). The
        nodedef is just the bag holding the property definitions; the
        editor reference is on the property itself, so the same control
        id can resolve to different editors — and different ranges —
        on different nodedefs.

        Callers should inspect both ``uom`` and ``max``:

        * ``uom == UOM_PERCENTAGE`` (51) → editor accepts 0-100 percent.
        * ``uom == UOM_8_BIT_RANGE`` (100) → editor accepts raw bytes;
          ``max`` tells you whether the device uses the full 0-255
          range (classic Insteon SwitchLinc) or a constrained subset
          like 0-100 (KeypadDimmer_ADV — byte-semantically but only the
          lower portion is valid).
        * ``names`` non-empty → enum / discrete values; the entity
          probably should be a SELECT rather than a NUMBER. The
          classifier currently maps controls to platforms statically
          (see helpers.NODE_AUX_FILTERS) and ignores this; the editor
          shape should drive that decision in a future refactor.

        Returns ``None`` when the editor can't be resolved.
        """
        if (nodedef := self._node.nodedef) is None:
            return None
        if (prop := nodedef.properties.get(control)) is None:
            return None
        editor = self._isy_data.root.profile.find_editor(
            prop.editor_id, self._node.family_id, self._node.instance_id
        )
        if editor is None or not editor.ranges:
            return None
        return editor.ranges[0]


class ISYProgramEntity(ISYEntity):
    """Representation of an IoX program base.

    Programs flow through the dedicated ``subscribe_program`` channel
    (control ``_1`` action ``"0"`` frames carrying the program id in
    ``<eventInfo>``) rather than the per-(addr, control) registry that
    nodes use, so we override ``async_added_to_hass`` to subscribe via
    that path.
    """

    # Programs are device-less in HA's model (they attach to the
    # controller hub stub). Their friendly name IS the program's
    # name — opt out of has_entity_name so HA doesn't prepend the
    # controller name (would yield "eisy.local Movie Mode").
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
        self._unsubscribers.append(
            self._isy_data.controller_events.subscribe_program(
                program.address, self._on_program_status
            )
        )

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
        """Get the state attributes for the device.

        Surfaces the actions / status program metadata pyisy 3.x
        consumers expected. The runtime ``Program`` wrapper exposes
        the timing fields as ISO 8601 strings; we keep them as-is
        here (the wrapper-side decode keeps the path symmetrical with
        the wire shape, and downstream automations that parsed the
        old strings still see strings).
        """
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
