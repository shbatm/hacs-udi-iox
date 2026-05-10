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

    @property
    def status(self) -> FakeNodePropertyValue | None:
        return self.properties.get("ST")


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
        self.programs: list[dict] = []
        self.variables: dict[str, list[dict]] = {"1": [], "2": []}
        self._event_listeners: list[Callable] = []
        self._lifecycle_listeners: list[Callable] = []
        self.refresh_calls = 0
        self.set_variable_value_calls: list[tuple] = []
        self.set_variable_init_calls: list[tuple] = []
        self.rename_variable_calls: list[tuple] = []

    def add_event_listener(self, callback: Callable) -> Callable[[], None]:
        self._event_listeners.append(callback)

        def _unsub() -> None:
            try:
                self._event_listeners.remove(callback)
            except ValueError:
                pass

        return _unsub

    def add_node_lifecycle_listener(
        self, callback: Callable
    ) -> Callable[[], None]:
        self._lifecycle_listeners.append(callback)

        def _unsub() -> None:
            try:
                self._lifecycle_listeners.remove(callback)
            except ValueError:
                pass

        return _unsub

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


@dataclass
class FakeLifecycleEvent:
    """Mimic pyisyox.NodeLifecycleEvent."""

    action: str
    node_address: str
    raw_action: str = ""
    seqnum: int = 0
    node_xml: str | None = None
