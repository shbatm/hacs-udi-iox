"""Representation of IoX entity types."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory
from homeassistant.core import Event as HassEvent
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription
from homeassistant.helpers.group import Group as HAEntityGroup
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
    Protocol,
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


def _group_device_info(
    isy_data: IsyData, group: Group, devices: dict[str, DeviceInfo]
) -> DeviceInfo | None:
    """DeviceInfo a scene entity attaches to.

    A single-controller scene links to that controller's device so the
    scene appears on that device's card; multi-controller (or
    controllerless) scenes return ``None`` and fall back to the hub
    (``ISYEntity.__init__`` stamps the hub identifiers). Shared by every
    scene entity platform (switch, binary_sensor, button, light) so the
    attachment rule can't silently diverge between them.
    """
    if group.controller_addresses and len(group.controller_addresses) == 1:
        controller_node = isy_data.root.nodes.get(group.controller_addresses[0])
        if controller_node is not None:
            return _resolve_device_info(devices, controller_node)
    return None


def aux_entity_category(control: str) -> EntityCategory | None:
    """HA placement policy for a coalesced aux control.

    A control that writes the node's primary status (``ST``) *is* the
    device's main control — a node-server ``virtualtemp`` setpoint and
    an i3 flags ``GV0`` "Mode" both coalesce to control id ``ST`` (via
    ``param.init="ST"``) — so it gets no category and sits with the
    primary controls. Every other aux control (on-level, backlight, the
    i3 ``GVx`` flags) is device configuration.
    """
    return None if control == PROP_STATUS else EntityCategory.CONFIG


def node_status_int(node: Node) -> int | None:
    """Read ``node.status`` as a scalar int (or ``None`` when unknown)."""
    prop = node.status
    if prop is None or not prop.value or prop.value == ISY_VALUE_UNKNOWN:
        return None
    try:
        return int(float(prop.value))
    except (ValueError, TypeError):
        return None


_NAME_TOKEN = re.compile(r"[^\s.:_-]+")


def _strip_parent_prefix(name: str, parent_name: str | None) -> str:
    """Strip the parent device's *leading token run* from a sub-node label.

    HA prepends the device name via ``has_entity_name``, so any leading
    tokens the sub-node repeats from its device double up
    (``"Hallway Keypad Hallway Keypad B"``).

    An exact ``str.startswith`` only catches the case where the device
    name is a verbatim prefix. IoX routinely names the device after the
    *primary* sub-node, which carries a distinguishing suffix the child
    lacks — ``"Kitchen Refrigerator Leak.Dry"`` (device) vs
    ``"Kitchen Refrigerator Leak HB"`` (child), or
    ``"Main Bedroom Fan Light"`` vs ``"Main Bedroom Ceiling Fan"`` — so
    the shared run is real but not a prefix. Compare token-wise instead
    (separators ``\\s . - : _``, case-insensitive) and slice the
    *original* string so the kept remainder's casing and punctuation
    survive (``"…B-Hallway"`` stays ``"KP.B-Hallway"``, not ``"KP B
    Hallway"``).
    """
    if not parent_name:
        return name
    parent_tokens = _NAME_TOKEN.findall(parent_name)
    child_tokens = list(_NAME_TOKEN.finditer(name))
    shared = 0
    for ptok, match in zip(parent_tokens, child_tokens, strict=False):
        if ptok.casefold() != match.group().casefold():
            break
        shared += 1
    # Nothing shared, or the child is wholly contained in the device
    # name — keep the full label rather than emit an empty/odd name.
    if not shared or shared >= len(child_tokens):
        return name
    return name[child_tokens[shared].start() :]


def _common_token_run(names: list[str]) -> int:
    """Count of leading tokens common (case-insensitive) to every name.

    Tokens are the same ``\\s . - : _``-delimited runs
    :func:`_strip_parent_prefix` uses. Returns ``0`` if the list is
    empty or any name has no tokens.
    """
    token_lists = [_NAME_TOKEN.findall(n) for n in names]
    if not token_lists or any(not toks for toks in token_lists):
        return 0
    shared = 0
    for group in zip(*token_lists, strict=False):
        first = group[0].casefold()
        if any(tok.casefold() != first for tok in group):
            break
        shared += 1
    return shared


def _strip_sensor_marker(name: str, marker: str) -> str:
    """Remove the configured ``sensor_string`` marker from a display name.

    Matched/stripped **verbatim** (exact substring) so the marker never
    leaks into a device name, entity name, entity_id, or the
    pnode-grouping token math, while classification still tests the raw
    IoX name. Whitespace left by a mid-name removal is collapsed; an
    empty result falls back to the original (a node named only by the
    marker keeps it rather than becoming nameless).
    """
    if not marker or marker not in name:
        return name
    stripped = " ".join(name.replace(marker, " ").split())
    return stripped or name


def _pnode_group_naming(
    nodes: dict[str, Node], primary: Node, sensor_string: str = ""
) -> tuple[str, str | None]:
    """Device name + primary-entity label for a pnode hardware group.

    IoX names each node of a multi-node device independently — a leak
    sensor is ``"<area> Leak.Dry"`` / ``".Wet"`` / ``".HB"``. Naming the
    HA device after the *primary* sub-node (``"… Leak.Dry"``) and
    leaving the primary entity unnamed makes the device card and the
    primary entity both read ``"… Leak.Dry"``.

    Instead, use the *shared leading token run* across the primary and
    its folded sub-nodes as the device name (``"… Leak"``) and the
    primary's residual suffix as its entity label (``"Dry"``).

    Returns ``(device_name, primary_label)``. ``primary_label`` is
    ``None`` — primary stays unnamed, inheriting the device name as
    before — unless **every member** (primary + folded sub-nodes)
    continues past the shared token prefix with a *non-space* separator
    (``-``/``.``/``_``/``:``) and a residual. That separator is IoX's
    "facet of one device" marker: a leak (``… Leak.Dry``/``.Wet``/
    ``.HB``), a motion sensor (``Motion Sensor-Sensor``/``-Dusk.Dawn``/
    ``-Low Bat``), a dual outlet (``Test Outlet.1 On-Off Top``/``.2
    On-Off Bot``) → device = shared prefix, primary entity = its
    residual. A plain space is just an ordinary compound name, left
    alone: ``Hallway Light`` + ``Hallway Button B``, a FanLinc
    ``FanLinc Lamp`` + ``FanLinc Motor``, a KeypadLinc whose primary
    *is* the prefix — those keep today's device name and the existing
    :func:`_strip_parent_prefix` child handling, no id churn.
    """
    # Folded sub-nodes only: node-server children carry a primary_address
    # but own their device, so they must not shorten the primary's name.
    # Operate on marker-stripped names so the sensor_string never
    # pollutes the shared-prefix math or the returned device/label.
    prim_name = _strip_sensor_marker(primary.name, sensor_string)
    subs = [
        n
        for n in nodes.values()
        if n.primary_address == primary.address
        and n.protocol != Protocol.NODE_SERVER
        and n.name
    ]
    if not subs:
        return prim_name, None
    members = [primary, *subs]
    names = [_strip_sensor_marker(m.name, sensor_string) for m in members]
    shared = _common_token_run(names)
    if shared < 1:
        return prim_name, None
    prim_cut = prim_res = 0
    for m, mname in zip(members, names, strict=False):
        toks = list(_NAME_TOKEN.finditer(mname))
        if len(toks) <= shared:
            return prim_name, None  # no residual (primary == prefix)
        gap = mname[toks[shared - 1].end() : toks[shared].start()]
        if not any(c in "-._:" for c in gap):
            return prim_name, None  # space-delimited compound name
        if m is primary:
            prim_cut, prim_res = toks[shared - 1].end(), toks[shared].start()
    return prim_name[:prim_cut], prim_name[prim_res:]


def _primary_status_label(
    node: Node, node_def: NodeDef | None, primary_label: str | None
) -> str | None:
    """Name for an own-device primary's status (``ST``) entity.

    The pnode-group residual wins (``"Dry"`` for a leak primary). Else,
    a *node-server* node whose nodedef names ``ST`` takes that name with
    the generic ``"Status"`` token stripped — ``"Current"`` stays
    ``"Current"``, ``"Switch Status"`` (a nodedef quirk) → ``"Switch"``,
    a bare ``"Status"`` → nothing → unnamed (the primary then just reads
    as the device, matching the eisy UI: device = node name). Native
    Insteon/Z-Wave is unnamed regardless (gated to node-server).
    """
    if primary_label is not None:
        return primary_label
    if node.protocol == Protocol.NODE_SERVER and node_def is not None:
        st = node_def.properties.get(PROP_STATUS)
        if st is not None and st.name:
            kept = [t for t in _NAME_TOKEN.findall(st.name) if t.casefold() != "status"]
            return " ".join(kept) or None
    return None


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
        # Match HA's ``Entity._attr_name: str | None``; subclasses below
        # legitimately reassign a possibly-None composed name.
        self._attr_name: str | None = _strip_sensor_marker(
            getattr(node, "name", "") or "", isy_data.sensor_string
        )
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


class _ISYSceneGroup(HAEntityGroup):
    """Resolve an ISY scene's member nodes to their HA entity_ids so
    they render in the scene's more-info dialog (HA's ``group_entities``
    capability attribute — same mechanism MQTT groups use).

    Core's ``IntegrationSpecificGroup`` resolves members under the group
    entity's *own* domain; ISY scene members span domains (light /
    switch / fan / …), so resolve by ``(platform, unique_id)`` across
    every domain instead. Mirrors core's registry-event invalidation so
    members that register after the scene still appear.
    """

    _member_entity_ids: list[str] | None = None

    def __init__(self, entity: Entity, member_unique_ids: list[str]) -> None:
        """Store the scene's member unique-ids (``{uuid}_{address}``)."""
        super().__init__(entity)
        self._member_unique_ids = member_unique_ids

    @property
    def member_entity_ids(self) -> list[str]:
        """Member entity_ids, resolved + cached (registry-keyed)."""
        if self._member_entity_ids is not None:
            return self._member_entity_ids
        registry = er.async_get(self._entity.hass)
        want = set(self._member_unique_ids)
        resolved = {
            entry.unique_id: entry.entity_id
            for entry in registry.entities.values()
            if entry.platform == DOMAIN and entry.unique_id in want
        }
        self._member_entity_ids = [
            resolved[uid] for uid in self._member_unique_ids if uid in resolved
        ]
        return self._member_entity_ids

    @callback
    def async_added_to_hass(self) -> None:
        """Register + invalidate the cache on member registry changes."""
        super().async_added_to_hass()
        entity = self._entity
        registry = er.async_get(entity.hass)
        want = set(self._member_unique_ids)

        @callback
        def _handle_registry_updated(event: HassEvent[Any]) -> None:
            action = event.data["action"]
            entity_id = event.data["entity_id"]
            if action in {"create", "update"}:
                entry = registry.async_get(entity_id)
                relevant = (
                    entry is not None
                    and entry.platform == DOMAIN
                    and entry.unique_id in want
                )
            elif action == "remove":
                relevant = (
                    self._member_entity_ids is not None
                    and entity_id in self._member_entity_ids
                )
            else:
                relevant = False
            if relevant:
                self._member_entity_ids = None
                entity.async_write_ha_state()

        entity.async_on_remove(
            entity.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, _handle_registry_updated
            )
        )


class ISYGroupEntity(ISYEntity):
    """Representation of a ISY Group entity."""

    # Scenes carry full descriptive names from the controller ("Living
    # Room Scene"). Opt out of has_entity_name so the friendly name
    # stays the scene name as-is, regardless of which device the entity
    # attaches to (controller hub for multi-controller scenes, or a
    # specific node device for single-controller scenes).
    _attr_has_entity_name = False
    _node: Group

    def __init__(
        self,
        isy_data: IsyData,
        node: Group,
        device_info: DeviceInfo | None = None,
        unique_id: str | None = None,
    ) -> None:
        """Expose the scene's members via HA's group framework.

        ``self.group`` must be set before ``async_internal_added_to_hass``
        runs (core binds it there), so do it in ``__init__``.
        """
        super().__init__(isy_data, node, device_info=device_info, unique_id=unique_id)
        uuid = isy_data.uuid
        member_unique_ids = [f"{uuid}_{address}" for address in node.member_addresses]
        if member_unique_ids:
            self.group = _ISYSceneGroup(self, member_unique_ids)

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
        # - Primary on a node owning its DeviceInfo → its residual suffix
        #   past the pnode-group's shared prefix ("Dry" for a leak's
        #   "… Leak.Dry" primary under device "… Leak"); else a
        #   node-server node's nodedef ST name ("Current" on a
        #   virtualtemp); else ``None`` (native primary / KeypadLinc →
        #   unnamed, inherits the device name).
        # - Primary on a folded sub-node → sub-name with parent prefix
        #   stripped (avoids duplicating the device prefix).
        # - Aux → property label or COMMAND_FRIENDLY_NAME, prefixed with
        #   the sub-name on folded sub-nodes so e.g. a sub-node "Ramp
        #   Rate" doesn't render identically to the parent's. On an
        #   own-device primary it stays the bare label on the (possibly
        #   shortened) device name — button/switch build their names
        #   outside this block, so this matches them.
        self._node_def = node.nodedef
        node_owns_device = (
            node.primary_address is None or node.protocol == Protocol.NODE_SERVER
        )
        primary_label = (
            _pnode_group_naming(isy_data.root.nodes, node, isy_data.sensor_string)[1]
            if node_owns_device
            else None
        )
        if control == PROP_STATUS:
            if node_owns_device:
                name: str | None = _primary_status_label(
                    node, self._node_def, primary_label
                )
            else:
                parent = isy_data.root.nodes.get(node.primary_address)
                parent_name = parent.name if parent is not None else None
                marker = isy_data.sensor_string
                name = _strip_parent_prefix(
                    _strip_sensor_marker(node.name, marker),
                    _strip_sensor_marker(parent_name, marker)
                    if parent_name is not None
                    else None,
                )
        else:
            label: str | None = None
            if self._node_def is not None and (
                prop := self._node_def.properties.get(control)
            ):
                label = prop.name or None
            if label is None and self._node_def is not None:
                # Aux entities are fanned out from cmds.accepts — prefer
                # the controller-published command name (SETOL → "Set On
                # Level") over a title-cased id ("Setol"). Plugin
                # nodedefs name their setters; only the built-in Insteon
                # set needs the COMMAND_FRIENDLY_NAME fallback below.
                label = next(
                    (
                        cmd.name
                        for cmd in self._node_def.cmds.accepts
                        if cmd.id == control and cmd.name
                    ),
                    None,
                )
            if label is None:
                label = (
                    COMMAND_FRIENDLY_NAME.get(control, control)
                    .replace("_", " ")
                    .title()
                )
            if node_owns_device:
                # Aux keeps its bare label on the (now possibly
                # shortened) device name — same across every platform
                # (button/switch build names independently of this
                # block, so prefixing here would desync them).
                name = label
            else:
                parent = isy_data.root.nodes.get(node.primary_address)
                parent_name = parent.name if parent is not None else None
                marker = isy_data.sensor_string
                prefix = _strip_parent_prefix(
                    _strip_sensor_marker(node.name, marker),
                    _strip_sensor_marker(parent_name, marker)
                    if parent_name is not None
                    else None,
                )
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
