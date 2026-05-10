"""Representation of IoX entity types."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription
from homeassistant.util.dt import as_local
from pyisyox import (
    Event,
    Folder,
    Group,
    Node,
    NodeLifecycleAction,
    NodeLifecycleEvent,
    NodePropertyValue,
)
from pyisyox.constants import (
    COMMAND_FRIENDLY_NAME,
    ISY_VALUE_UNKNOWN,
    PROP_STATUS,
    Protocol,
)
from pyisyox.schema.nodedef import NodeDef

from .const import DOMAIN

if TYPE_CHECKING:
    from .controller_events import IsyControllerEvents
    from .models import IsyData, ProgramRecord, VariableRecord

# PEP 695 lazy type aliases — the right-hand side is evaluated only when
# the alias is consumed, so the (TYPE_CHECKING-only) ProgramRecord /
# VariableRecord references don't pull models.py at import time. Keeps
# the import graph acyclic to match the HA Core isy994 layout.
type NodeType = Node | Group | Folder | ProgramRecord | VariableRecord
type NodeEventType = NodePropertyValue | NodeLifecycleEvent


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


class ISYEntity(Entity):
    """Base class for IoX entities backed by a runtime Node."""

    _attr_has_entity_name = False
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

    _node: Group

    @property
    def extra_state_attributes(self) -> dict:
        """Get the state attributes for the device."""
        return {"group_all_on": self._node.group_all_on}


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
        super().__init__(
            isy_data, node, device_info=device_info, unique_id=unique_id
        )
        self._control = control
        if description is not None:
            self.entity_description = description

        # Determine the entity or device name to use. v6 NodeDef stores
        # property labels per-property at nodedef.properties[id].name;
        # the legacy status_names dict is gone.
        name: str | None = None
        self._node_def = node.nodedef
        if self._node_def is not None and (
            prop := self._node_def.properties.get(control)
        ):
            name = prop.name or None
        if name is None and control != PROP_STATUS:
            name = COMMAND_FRIENDLY_NAME.get(control, control).replace("_", " ").title()

        if node.parent_address is not None:
            name = f"{node.name} {name}" if name else node.name
            self._attr_has_entity_name = False
        else:
            self._attr_has_entity_name = True

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
        self._unsubscribers.append(
            events.subscribe_lifecycle(self._on_lifecycle)
        )

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

    async def async_send_node_command(self, command: str) -> None:
        """Respond to an entity service command call.

        The legacy v3 ``send_node_command`` service mapped friendly
        names ("brighten", "fast_off") onto Node helper methods. v6
        unifies on Node.send_command — translate friendly names to the
        canonical IoX command id and let the editor codec validate.
        """
        from pyisyox.constants import COMMAND_FRIENDLY_NAME

        # Reverse the friendly-name → IoX-id map.
        friendly_to_id = {v: k for k, v in COMMAND_FRIENDLY_NAME.items()}
        cmd_id = friendly_to_id.get(command, command)
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
        params = (value,) if value is not None else ()
        await self._node.send_command(command, *params)

    async def async_get_zwave_parameter(self, parameter: int) -> None:
        """Z-Wave parameter read — not supported."""
        raise HomeAssistantError(
            "Z-Wave parameter services are not supported"
        )

    async def async_set_zwave_parameter(
        self, parameter: int, value: int, size: int
    ) -> None:
        """Z-Wave parameter write — not supported."""
        raise HomeAssistantError(
            "Z-Wave parameter services are not supported"
        )

    async def async_rename_node(self, name: str) -> None:
        """Rename the underlying node — not supported."""
        raise HomeAssistantError(
            "Renaming nodes from HA is not supported"
        )


class ISYProgramEntity(ISYEntity):
    """Representation of an IoX program base."""

    _actions: ProgramRecord | None
    _status: ProgramRecord

    def __init__(
        self,
        isy_data: IsyData,
        name: str,
        status: ProgramRecord,
        actions: ProgramRecord | None = None,
    ) -> None:
        """Initialize the program-based entity."""
        super().__init__(isy_data, status)
        self._attr_name = name
        self._actions = actions

    @property
    def extra_state_attributes(self) -> dict:
        """Get the state attributes for the device."""
        # Programs are raw dicts; extract known fields with .get() so
        # missing keys don't blow up.
        attr: dict[str, Any] = {}
        if self._actions:
            attr["actions_enabled"] = self._actions.get("enabled")
            for key in ("last_finish_time", "last_run_time", "last_update"):
                value = self._actions.get(key)
                if value is not None:
                    attr[f"actions_{key.replace('_time', '')}"] = str(as_local(value))
            attr["run_at_startup"] = self._actions.get("run_at_startup")
            attr["running"] = self._actions.get("running")

        status = self._node if isinstance(self._node, dict) else {}
        attr["status_enabled"] = status.get("enabled")
        for key in ("last_finish_time", "last_run_time", "last_update"):
            value = status.get(key)
            if value is not None:
                attr[f"status_{key.replace('_time', '')}"] = str(as_local(value))
        return attr
