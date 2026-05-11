"""In-process stubs that mimic pyisyox's public surface.

Lives outside ``conftest.py`` so test modules can import the dataclasses
directly (pytest's conftest mechanism doesn't expose conftest as a
regular module — fixtures are wired through pytest's request/yield
plumbing instead).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeNodePropertyValue:
    """Mimic pyisyox.NodePropertyValue."""

    id: str
    value: str = ""
    formatted: str = ""
    uom: str = ""
    name: str = ""
    prec: int = 0


@dataclass
class FakeNode:
    """Stand-in for pyisyox.Node.

    Field names mirror the real surface so production code under test
    reads the attributes it expects without monkeypatching.
    """

    address: str
    name: str = "Test Node"
    family_id: str = "1"
    instance_id: str = "1"
    nodedef_id: str = ""
    type: str = "1.0.0.0"
    parent_address: str | None = None
    enabled: bool = True
    protocol: str = "insteon"
    is_thermostat: bool = False
    is_lock: bool = False
    is_fan: bool = False
    is_dimmable: bool = False
    is_battery_node: bool = False
    nodedef: Any | None = None
    properties: dict[str, FakeNodePropertyValue] = field(default_factory=dict)
    primary_node: str | None = None

    def __post_init__(self) -> None:
        if self.primary_node is None:
            self.primary_node = self.parent_address
        # Initialised here rather than as a default field so each
        # FakeNode gets a fresh list (avoids the dataclass-mutable-default
        # pitfall) without requiring callers to pass it.
        self.rename_calls: list[str] = []

    @property
    def status(self) -> FakeNodePropertyValue | None:
        return self.properties.get("ST")

    async def rename(self, new_name: str) -> None:
        """Mirror ``pyisyox.Node.rename``."""
        self.rename_calls.append(new_name)
        self.name = new_name


@dataclass
class FakeNetworkResource:
    """Stand-in for ``pyisyox.NetworkResource`` (typed wrapper)."""

    address: str
    name: str = "Test Resource"

    def __post_init__(self) -> None:
        self.run_calls: list[None] = []

    async def run(self) -> None:
        self.run_calls.append(None)


@dataclass
class FakeGroup:
    """Stand-in for ``pyisyox.Group`` (scene wrapper).

    Mirrors only the fields the consumer's switch / scene code reads:
    ``address``, ``name``, the two aggregates (``group_any_on`` /
    ``group_all_on``), and ``controller_addresses`` (used to link the
    scene's HA device to its controller node). The real Group derives
    the aggregates from the nodes registry; here they're static — set
    them directly per test.
    """

    address: str
    name: str = "Test Scene"
    group_any_on: bool = False
    group_all_on: bool = False
    controller_addresses: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rename_calls: list[str] = []
        self.send_command_calls: list[tuple] = []

    async def rename(self, new_name: str) -> None:
        self.rename_calls.append(new_name)
        self.name = new_name

    async def send_command(self, command_id: str, *params: int) -> None:
        self.send_command_calls.append((command_id, *params))


@dataclass
class FakeProgram:
    """Stand-in for ``pyisyox.Program`` (typed wrapper).

    Field names mirror the real surface so production code reads
    attributes it expects without monkeypatching. The ``run`` /
    ``run_then`` / ``run_else`` methods record calls into separate
    lists per verb so service-layer + entity-layer tests can assert
    on routing.
    """

    address: str
    name: str = "Test Program"
    path: str = ""
    status: bool = False
    enabled: bool | None = True
    run_at_startup: bool | None = False
    running: str | None = "idle"
    last_run_time: str | None = None
    last_finish_time: str | None = None
    next_scheduled_run_time: str | None = None
    parent_address: str | None = None

    def __post_init__(self) -> None:
        self.run_calls: list[None] = []
        self.run_then_calls: list[None] = []
        self.run_else_calls: list[None] = []
        self.stop_calls: list[None] = []

    async def run(self) -> None:
        self.run_calls.append(None)

    async def run_then(self) -> None:
        self.run_then_calls.append(None)

    async def run_else(self) -> None:
        self.run_else_calls.append(None)

    async def stop(self) -> None:
        self.stop_calls.append(None)


class FakeController:
    """Minimal Controller stand-in.

    Records every callback registered via ``add_event_listener`` /
    ``add_node_lifecycle_listener`` so tests can fire synthetic
    events. Implements the async write methods that services.py +
    number.py call so service-layer tests don't need to mock pyisyox
    at the module level.
    """

    def __init__(self, uuid: str = "test-uuid") -> None:
        self.config = SimpleNamespace(uuid=uuid, version="6.0.0a1")
        self.connected = True
        self.nodes: dict[str, Any] = {}
        self.groups: dict[str, Any] = {}
        self.programs: dict[str, FakeProgram] = {}
        self.program_folders: dict[str, FakeProgram] = {}
        self.variables: dict[str, list[dict]] = {"1": [], "2": []}
        self._event_listeners: list[Callable] = []
        self._lifecycle_listeners: list[Callable] = []
        self._program_status_listeners: list[Callable] = []
        self.refresh_calls = 0
        self.set_variable_value_calls: list[tuple] = []
        self.set_variable_init_calls: list[tuple] = []
        self.rename_variable_calls: list[tuple] = []
        self.network_resources: dict[str, FakeNetworkResource] = {}
        self.run_network_resource_calls: list[str | int] = []
        self.send_program_command_calls: list[tuple[str, str]] = []
        # ``websocket`` mirrors the real ``Controller.websocket`` property.
        # Tests that need WS-health surfaces set this to a SimpleNamespace
        # with ``status`` (StrEnum-like .value) and ``last_event_at`` —
        # see test_system_health.py. None by default to mirror the
        # ``start_websocket=False`` case.
        self.websocket: Any = None

    def add_event_listener(self, callback: Callable) -> Callable[[], None]:
        self._event_listeners.append(callback)

        def _unsub() -> None:
            try:
                self._event_listeners.remove(callback)
            except ValueError:
                pass

        return _unsub

    def add_node_lifecycle_listener(self, callback: Callable) -> Callable[[], None]:
        self._lifecycle_listeners.append(callback)

        def _unsub() -> None:
            try:
                self._lifecycle_listeners.remove(callback)
            except ValueError:
                pass

        return _unsub

    def add_program_status_listener(self, callback: Callable) -> Callable[[], None]:
        self._program_status_listeners.append(callback)

        def _unsub() -> None:
            try:
                self._program_status_listeners.remove(callback)
            except ValueError:
                pass

        return _unsub

    def fire_program_status(self, event: Any) -> None:
        """Fan a ProgramStatusEvent-shaped object to listeners."""
        for listener in tuple(self._program_status_listeners):
            listener(event)

    async def send_program_command(self, program_id: str, command: str) -> None:
        """Mirror ``Controller.send_program_command``."""
        self.send_program_command_calls.append((program_id, command))

    def fire_event(self, event: Any) -> None:
        """Fan an Event-shaped object to all event listeners."""
        for listener in tuple(self._event_listeners):
            listener(event)

    def fire_lifecycle(self, event: Any) -> None:
        """Fan a NodeLifecycleEvent-shaped object to lifecycle listeners."""
        for listener in tuple(self._lifecycle_listeners):
            listener(event)

    async def refresh(self) -> None:
        self.refresh_calls += 1

    async def set_variable_value(
        self, var_type: int | str, var_id: int | str, value: int
    ) -> None:
        self.set_variable_value_calls.append((var_type, var_id, value))

    async def set_variable_init(
        self, var_type: int | str, var_id: int | str, init: int
    ) -> None:
        self.set_variable_init_calls.append((var_type, var_id, init))

    async def rename_variable(
        self, var_type: int | str, var_id: int | str, name: str
    ) -> None:
        self.rename_variable_calls.append((var_type, var_id, name))

    async def run_network_resource(self, resource_id: str | int) -> None:
        """Mirror ``Controller.run_network_resource``."""
        self.run_network_resource_calls.append(resource_id)

    async def connect(self, *, start_websocket: bool = True) -> None:
        """No-op stand-in for ``Controller.connect``.

        Real ``connect`` blocks on /api/nodes + /api/programs + WS startup;
        tests pre-populate ``self.nodes`` / ``self.programs`` etc. directly
        so this only needs to satisfy the call site in ``async_setup_entry``.
        """
        self.connected = True

    async def stop(self) -> None:
        self._event_listeners.clear()
        self._lifecycle_listeners.clear()


@dataclass
class FakeEvent:
    """Mimic pyisyox.Event."""

    seqnum: int = 0
    timestamp: str = ""
    control: str = ""
    action: str = ""
    node_address: str = ""
    formatted_action: str = ""
    formatted_name: str = ""
    uom: str = ""
    prec: int | None = None
    event_info: str = ""


_RELOAD_REQUIRED_VERBS = frozenset({"ND", "NR", "NN", "RG", "EN", "RV"})


@dataclass
class FakeLifecycleEvent:
    """Mimic pyisyox.NodeLifecycleEvent."""

    action: str
    node_address: str
    raw_action: str = ""
    seqnum: int = 0
    node_xml: str | None = None

    @property
    def requires_reload(self) -> bool:
        """Mirror pyisyox NodeLifecycleEvent.requires_reload."""
        return self.action in _RELOAD_REQUIRED_VERBS
