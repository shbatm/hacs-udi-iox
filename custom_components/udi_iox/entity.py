"""Representation of ISYEntity Types."""

from __future__ import annotations

from typing import Any, TypeAlias

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription
from homeassistant.util.dt import as_local
from pyisyox import (
    EventListener,
    Folder,
    Group,
    Node,
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
from .models import ProgramRecord, VariableRecord

NodeType: TypeAlias = Node | Group | Folder | ProgramRecord | VariableRecord
NodeEventType: TypeAlias = NodePropertyValue | NodeLifecycleEvent


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
    """Representation of an ISY device."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _node: NodeType
    _change_handler: EventListener

    def __init__(
        self,
        node: NodeType,
        device_info: DeviceInfo | None = None,
        unique_id: str | None = None,
    ) -> None:
        """Initialize the ISY/IoX entity."""
        self._node = node
        self._attr_name = node.name
        if device_info is None:
            device_info = DeviceInfo(identifiers={(DOMAIN, node.isy.uuid)})
        self._attr_device_info = device_info
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{node.isy.uuid}_{node.address}"
        )
        self._attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to the node change events."""
        self._change_handler = self._node.status_events.subscribe(
            self.async_on_update, key=self.unique_id
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from node events."""
        self._change_handler.unsubscribe()

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle the update event from the ISY Node."""
        self.async_write_ha_state()


class ISYGroupEntity(ISYEntity):
    """Representation of a ISY Group entity."""

    _node: Group

    @property
    def extra_state_attributes(self) -> dict:
        """Get the state attributes for the device."""
        return {"group_all_on": self._node.group_all_on}


class ISYNodeEntity(ISYEntity):
    """Representation of a ISY Node entity."""

    _node: Node
    _control: str
    _node_def: NodeDef | None = None
    _change_handler: EventListener
    _availability_handler: EventListener

    def __init__(
        self,
        node: Node,
        control: str = PROP_STATUS,
        unique_id: str | None = None,
        description: EntityDescription | None = None,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the ISY/IoX node entity."""
        super().__init__(node, device_info=device_info, unique_id=unique_id)
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
        """Subscribe to the node control change events."""
        self._change_handler = self._node.control_events.subscribe(
            self.async_on_update,
            event_filter={ATTR_CONTROL: self._control},
            key=self.unique_id,
        )
        self._availability_handler = self._node.isy.nodes.platform_events.subscribe(
            self.async_on_update,
            event_filter={
                TAG_ADDRESS: self._node.address,
                ATTR_ACTION: NodeChangeAction.NODE_ENABLED,
            },
            key=self.unique_id,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from node events."""
        self._change_handler.unsubscribe()
        self._availability_handler.unsubscribe()

    @callback
    def async_on_update(self, event: NodeEventType, key: str) -> None:
        """Handle a control event from the ISY Node."""
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
        """Respond to an entity service raw command call.

        pyisyox 6 routes all commands through Node.send_command, which is
        editor-validated. ``unit_of_measurement`` and ``parameters`` from
        the legacy v3 surface no longer apply — the codec resolves them
        from the node's profile. The service is kept for backwards
        compatibility but only the (command, value) pair is honored.
        """
        params = (value,) if value is not None else ()
        await self._node.send_command(command, *params)

    async def async_get_zwave_parameter(self, parameter: int) -> None:
        """Respond to service: request a Z-Wave device parameter."""
        # Z-Wave parameter REST surface is deferred in pyisyox 6.0.0a1;
        # /rest/zwave/* hasn't been verified against a live capture yet.
        raise HomeAssistantError(
            "Z-Wave parameter services are not supported in this release"
        )

    async def async_set_zwave_parameter(
        self, parameter: int, value: int, size: int
    ) -> None:
        """Respond to service: set a Z-Wave device parameter."""
        raise HomeAssistantError(
            "Z-Wave parameter services are not supported in this release"
        )

    async def async_rename_node(self, name: str) -> None:
        """Rename the underlying IoX node.

        pyisyox 6 doesn't expose a per-Node rename helper yet; the
        controller-level wrapper is the path forward.
        """
        raise HomeAssistantError(
            "Renaming nodes from HA is not yet supported in this release"
        )


class ISYProgramEntity(ISYEntity):
    """Representation of an ISY program base."""

    _actions: ProgramRecord | None
    _status: ProgramRecord

    def __init__(
        self,
        name: str,
        status: ProgramRecord,
        actions: ProgramRecord | None = None,
    ) -> None:
        """Initialize the program-based entity."""
        super().__init__(status)
        self._attr_name = name
        self._actions = actions

    @property
    def extra_state_attributes(self) -> dict:
        """Get the state attributes for the device."""
        # Programs are exposed as raw dicts in pyisyox 6.0.0a1; extract
        # known fields with .get() so missing keys don't blow up.
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
