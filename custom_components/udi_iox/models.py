"""The IoX integration data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers.entity import DeviceInfo
from pyisyox import (
    Controller,
    Group,
    NetworkResource,
    Node,
    NodePropertyValue,
    Program,
    Variable,
)
from pyisyox.constants import Protocol
from pyisyox.schema.nodedef import Command

from .const import (
    CONF_NETWORK,
    NODE_AUX_PROP_PLATFORMS,
    NODE_PARALLEL_PLATFORMS,
    NODE_PLATFORMS,
    PROGRAM_PLATFORMS,
    ROOT_NODE_PLATFORMS,
    VARIABLE_PLATFORMS,
)
from .event import EVENT_BUTTON_UNIQUE_ID_SUFFIX
from .program_device import program_device_unique_ids

if TYPE_CHECKING:
    from .controller_events import IsyControllerEvents


@dataclass
class IsyData:
    """Data for the IoX integration."""

    root: Controller
    nodes: dict[Platform, list[Node]]
    groups: list[Group]
    root_nodes: dict[Platform, list[Node]]
    variables: dict[Platform, list[Variable]]
    programs: dict[Platform, list[tuple[str, Program, Program | None]]]
    net_resources: list[NetworkResource]
    devices: dict[str, DeviceInfo]
    aux_properties: dict[Platform, list[tuple[Node, str]]]
    # Per-EVENT-node trigger vocabulary: node address -> the wire command
    # ids the node emits (native Insteon press/fast/fade verbs, or a PG3
    # plugin's ``cmds.sends`` verbs). Consumed by ``event.py`` to derive
    # each entity's ``event_types``.
    node_triggers: dict[str, list[Command]]
    # Programs surfaced as their own HA devices — every program *outside*
    # the legacy ``HA.<platform>/<name>/{status,actions}`` switch
    # convention. Each one fans out into one binary sensor, one running
    # sensor, three timestamp sensors, two switches, an event entity, and
    # five buttons. See ``program_device.py``.
    program_devices: list[Program]
    controller_events: IsyControllerEvents

    def __init__(self) -> None:
        """Initialize an empty IoX data class."""
        self.nodes = {p: [] for p in (*NODE_PLATFORMS, *NODE_PARALLEL_PLATFORMS)}
        self.groups = []
        self.root_nodes = {p: [] for p in ROOT_NODE_PLATFORMS}
        self.aux_properties = {p: [] for p in NODE_AUX_PROP_PLATFORMS}
        self.programs = {p: [] for p in PROGRAM_PLATFORMS}
        self.variables = {p: [] for p in VARIABLE_PLATFORMS}
        self.net_resources = []
        self.devices = {}
        self.node_triggers = {}
        self.program_devices = []

    @property
    def uuid(self) -> str:
        """Return the controller UUID identification."""
        return self.root.config.uuid

    def uid_base(
        self,
        node: Node | Group | NetworkResource | NodePropertyValue | Variable | Program,
    ) -> str:
        """Return the unique id base string for a given node."""
        if isinstance(node, NetworkResource):
            return f"{self.uuid}_{CONF_NETWORK}_{node.address}"
        return f"{self.uuid}_{node.address}"

    @property
    def unique_ids(self) -> set[tuple[Platform, str]]:
        """Return all the unique ids for a config entry id."""
        current_unique_ids: set[tuple[Platform, str]] = {
            (Platform.BUTTON, f"{self.uuid}_query")
        }

        # Structure and prefixes here must match what's added in __init__ and helpers
        for platform in NODE_PLATFORMS:
            for node in self.nodes[platform]:
                current_unique_ids.add((platform, self.uid_base(node)))

        for group in self.groups:
            current_unique_ids.add((Platform.SWITCH, self.uid_base(group)))

        for platform in NODE_AUX_PROP_PLATFORMS:
            for node, control in self.aux_properties[platform]:
                current_unique_ids.add((platform, f"{self.uid_base(node)}_{control}"))

        for platform in PROGRAM_PLATFORMS:
            for _, program, _ in self.programs[platform]:
                current_unique_ids.add((platform, self.uid_base(program)))

        for platform in VARIABLE_PLATFORMS:
            for variable in self.variables[platform]:
                current_unique_ids.add((platform, self.uid_base(variable)))
                if platform == Platform.NUMBER:
                    current_unique_ids.add(
                        (platform, f"{self.uid_base(variable)}_init")
                    )

        for platform in ROOT_NODE_PLATFORMS:
            for node in self.root_nodes[platform]:
                current_unique_ids.add((platform, f"{self.uid_base(node)}_query"))
                if platform == Platform.BUTTON and node.protocol == Protocol.INSTEON:
                    current_unique_ids.add((platform, f"{self.uid_base(node)}_beep"))

        for resource in self.net_resources:
            current_unique_ids.add((Platform.BUTTON, self.uid_base(resource)))

        # EVENT-specific unique-id format. If more NODE_PARALLEL_PLATFORMS
        # are added with their own suffixes, generalize this loop to dispatch
        # by platform.
        for node in self.nodes[Platform.EVENT]:
            current_unique_ids.add(
                (
                    Platform.EVENT,
                    f"{self.uid_base(node)}{EVENT_BUTTON_UNIQUE_ID_SUFFIX}",
                )
            )

        # Per-program-device entity fan-out: each surfaced program adds
        # one binary sensor + four sensors + two switches + five buttons
        # + one event entity under its own HA device.
        current_unique_ids |= program_device_unique_ids(self)

        return current_unique_ids


type IsyConfigEntry = ConfigEntry[IsyData]
