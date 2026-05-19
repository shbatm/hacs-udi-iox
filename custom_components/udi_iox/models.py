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
    # Scenes whose IoX name carries the ``sensor_string`` marker. Forced
    # read-only: surfaced as ``binary_sensor`` (group state is on/off
    # only) instead of the default ``switch`` so a user can mark a
    # fire-and-observe scene (e.g. a garbage-disposal "scene") without
    # exposing a turn-on/off control. Mirrors the node sensor-marker
    # short-circuit in ``helpers._categorize_nodes`` (hacs-udi-iox#84).
    group_sensors: list[Group]
    # Scenes with no state-maintained member (pyisyox
    # ``Group.has_state_target`` is False — only fire-and-forget links,
    # no native responder the controller tracks). They have no
    # meaningful on/off state, so they're momentary ``button`` entities
    # (press = activate) rather than a switch stuck "on" forever
    # (hacs-udi-iox#86).
    group_buttons: list[Group]
    # State-maintained scenes with at least one dimmable member
    # (pyisyox ``Group.has_dimmable_members``). Modeled as an on/off
    # ``light`` so they land in HA's light domain natively — preserving
    # light semantics + the scene-member more-info framework without a
    # ``switch_as_x`` wrapper. Scenes carry no settable brightness, so
    # it's on/off only (hacs-udi-iox#86).
    group_lights: list[Group]
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
    # sensor, three timestamp sensors, two switches, and five buttons.
    # See ``program_device.py``.
    program_devices: list[Program]
    # The configured ``sensor_string`` marker (CONF_SENSOR_STRING).
    # Tested verbatim on the raw IoX name for forced sensor
    # classification AND stripped from device/entity display names so
    # the marker never leaks into the UI / entity_id. Empty = no strip
    # (test fixtures that don't set it).
    sensor_string: str
    controller_events: IsyControllerEvents

    def __init__(self) -> None:
        """Initialize an empty IoX data class."""
        self.nodes = {p: [] for p in (*NODE_PLATFORMS, *NODE_PARALLEL_PLATFORMS)}
        self.groups = []
        self.group_sensors = []
        self.group_buttons = []
        self.group_lights = []
        self.root_nodes = {p: [] for p in ROOT_NODE_PLATFORMS}
        self.aux_properties = {p: [] for p in NODE_AUX_PROP_PLATFORMS}
        self.programs = {p: [] for p in PROGRAM_PLATFORMS}
        self.variables = {p: [] for p in VARIABLE_PLATFORMS}
        self.net_resources = []
        self.devices = {}
        self.node_triggers = {}
        self.program_devices = []
        self.sensor_string = ""

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

        for group in self.group_sensors:
            current_unique_ids.add((Platform.BINARY_SENSOR, self.uid_base(group)))

        for group in self.group_buttons:
            current_unique_ids.add((Platform.BUTTON, self.uid_base(group)))

        for group in self.group_lights:
            current_unique_ids.add((Platform.LIGHT, self.uid_base(group)))

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
