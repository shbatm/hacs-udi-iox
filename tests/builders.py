"""Test builders that produce *real* pyisyox types.

Replaces the local ``Fake*`` dataclasses for tests that drive
``async_setup_entry`` (snapshot tests, integration smoke tests). Using
real ``Node`` / ``Group`` / ``Program`` / ``NetworkResource`` /
``Variable`` instances means:

* the consumer's reads exercise the actual pyisyox attribute surface —
  if pyisyox renames or retypes a field, the consumer tests fail
  immediately instead of via a drifted parallel fake;
* introspection (``is_thermostat``, ``is_lock``, ``is_dimmable``,
  ``is_fan``) flows through the real classifier-on-Node path, which
  consults the resolved nodedef + editor codec from a bundled real
  profile capture;
* anonymized fixtures are shared with pyisyox upstream — the consumer
  ships a copy of the eisy6 profile so the test path doesn't require
  pyisyox source-tree access (production HA installs pyisyox via pip).

This module is intentionally append-only: when a snapshot test needs a
new device family, add a builder here rather than open-coding a
``NodeRecord`` inside the test file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pyisyox import (
    Auth,
    Controller,
    Folder,
    Group,
    IoXClient,
    NetworkResource,
    Node,
    Program,
    Variable,
)
from pyisyox.client import (
    ControllerConfig,
    FolderRecord,
    GroupRecord,
    LoadResult,
    NetworkResourceRecord,
    NodePropertyValue,
    NodeRecord,
    ProgramRecord,
    VariableRecord,
)
from pyisyox.runtime.events import EventDispatcher
from pyisyox.schema import Profile

FIXTURE_DIR = Path(__file__).parent / "fixtures"
DEFAULT_UUID = "aa:bb:cc:dd:ee:ff"
DEFAULT_HOST = "http://eisy.local:8080"


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    """Bundled anonymized eisy6 profile — contains the nodedefs the consumer's
    classifier resolves (DimmerLampSwitch / FanLincMotor / Thermostat /
    DoorLock / KeypadDimmer / etc.).

    Cached because the JSON blob is ~340 KB and parse cost shows up under
    pytest-xdist.
    """
    raw = json.loads((FIXTURE_DIR / "eisy6_profile.json").read_text())
    return Profile.load_from_json(raw)


# ---------------------------------------------------------------------------
# Record builders — wire-shape dataclasses.
# ---------------------------------------------------------------------------


def make_node_record(
    address: str,
    name: str,
    *,
    nodedef_id: str = "DimmerLampSwitch",
    family_id: str = "1",
    instance_id: str = "1",
    type_: str = "1.0.0.0",
    parent_address: str | None = None,
    pnode: str | None = None,
    enabled: bool = True,
    properties: dict[str, NodePropertyValue] | None = None,
    status_value: str = "0",
    status_uom: str = "100",
    status_formatted: str = "Off",
    status_prec: int = 0,
) -> NodeRecord:
    """Build a minimal :class:`NodeRecord`.

    ``status_*`` kwargs are a shortcut for the always-present ``ST``
    property. Override ``properties`` to take full control (e.g. plugin
    nodes that don't carry a status, or thermostat setpoint properties).

    ``pnode`` defaults to the **node's own address** when neither
    ``pnode`` nor ``parent_address`` is supplied — that's the wire
    convention for Insteon device roots (the primary node is the
    device itself). For sub-nodes, ``parent_address`` flows through
    automatically.
    """
    if properties is None:
        properties = {
            "ST": NodePropertyValue(
                id="ST",
                value=status_value,
                formatted=status_formatted,
                uom=status_uom,
                name="Status",
                prec=status_prec,
            ),
        }
    return NodeRecord(
        address=address,
        name=name,
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        type=type_,
        parent_address=parent_address,
        pnode=pnode or parent_address or address,
        enabled=enabled,
        properties=properties,
    )


def make_group_record(
    address: str,
    name: str,
    *,
    nodedef_id: str = "InsteonDimmer",
    family_id: str = "6",
    instance_id: str = "1",
    member_addresses: tuple[str, ...] = (),
    controller_addresses: tuple[str, ...] = (),
) -> GroupRecord:
    return GroupRecord(
        address=address,
        name=name,
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        member_addresses=member_addresses,
        controller_addresses=controller_addresses,
    )


def make_folder_record(
    address: str, name: str, *, parent_address: str | None = None
) -> FolderRecord:
    return FolderRecord(address=address, name=name, parent_address=parent_address)


def make_program_record(
    address: str,
    name: str,
    *,
    path: str = "",
    status: bool = False,
    enabled: bool | None = True,
    is_folder: bool = False,
    parent_address: str | None = None,
) -> ProgramRecord:
    return ProgramRecord(
        address=address,
        name=name,
        path=path,
        status=status,
        enabled=enabled,
        is_folder=is_folder,
        parent_address=parent_address,
    )


def make_network_resource_record(address: str, name: str) -> NetworkResourceRecord:
    return NetworkResourceRecord(address=address, name=name)


def make_variable_record(
    type_id: str,
    id_: str,
    name: str,
    *,
    value: int = 0,
    init: int = 0,
    prec: int = 0,
    ts: str = "",
) -> VariableRecord:
    return VariableRecord(
        type_id=type_id, id=id_, name=name, value=value, init=init, prec=prec, ts=ts
    )


# ---------------------------------------------------------------------------
# Controller wiring.
# ---------------------------------------------------------------------------


def make_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
    groups: dict[str, GroupRecord] | None = None,
    folders: dict[str, FolderRecord] | None = None,
    programs: dict[str, ProgramRecord] | None = None,
    variables: dict[str, dict[str, VariableRecord]] | None = None,
    network_resources: dict[str, NetworkResourceRecord] | None = None,
) -> LoadResult:
    """Assemble a :class:`LoadResult` shaped like a real ``IoXClient.connect()``
    output — but populated directly without HTTP.

    The profile is shared (the bundled anonymized capture) so node
    introspection (``is_thermostat`` / ``is_lock`` / ``is_dimmable``) and
    editor-codec command validation work the same way they do at runtime.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=load_profile(),
        nodes=nodes or {},
        groups=groups or {},
        folders=folders or {},
        programs=programs or {},
        triggers=[],
        variables=variables or {"1": {}, "2": {}},
        network_resources=network_resources or {},
    )


def make_controller(
    load_result: LoadResult,
    *,
    host: str = DEFAULT_HOST,
) -> Controller:
    """Return a real :class:`Controller` with ``_loaded`` + ``_dispatcher``
    pre-populated — ``connect()`` is a no-op so the test never touches
    the network.

    The ``_client`` is set to a ``MagicMock`` shaped after :class:`IoXClient`
    so mutation methods (``set_variable_value``, ``post_node_update``,
    ``send_node_command``, etc.) return successfully without dispatching
    real HTTP. Tests that assert on call shape patch / mock the client
    methods they care about.

    ``websocket`` stays None (matches ``start_websocket=False`` loads); set
    ``controller._ws`` directly if a test needs the WS-health rows.
    """
    auth_stub = MagicMock(spec=Auth)
    session_stub = MagicMock()
    controller = Controller(host, auth=auth_stub, session=session_stub)
    controller._loaded = load_result

    client = _build_fake_client(host, auth_stub, session_stub)
    controller._client = client
    controller._dispatcher = EventDispatcher(
        load_result.nodes, programs=load_result.programs
    )
    return controller


def _build_fake_client(host: str, auth: Any, session: Any) -> IoXClient:
    """A real :class:`IoXClient` with HTTP methods stubbed.

    Keeps the real class so ``isinstance(client, IoXClient)`` holds and
    method signatures stay typed; only the HTTP-dispatching coroutines
    are replaced with ``AsyncMock``s that succeed silently.
    """
    client = IoXClient(host, auth, session)
    client._authenticated = True

    for method_name in (
        "send_node_command",
        "post_node_update",
        "post_variable_update",
        "send_program_command",
        "run_network_resource",
    ):
        setattr(client, method_name, AsyncMock(return_value=None))

    return client


# ---------------------------------------------------------------------------
# High-level wrappers — load_result + controller in one call.
# ---------------------------------------------------------------------------


def make_node(record: NodeRecord, controller: Controller) -> Node:
    """Real :class:`Node` resolved against the controller's profile + client."""
    return Node.from_record(record, controller._loaded.profile, controller._client)


def make_group(
    record: GroupRecord,
    controller: Controller,
    nodes: dict[str, NodeRecord] | None = None,
) -> Group:
    """Real :class:`Group` bound to the controller's profile + client.

    Pass ``nodes`` to enable the ``group_all_on`` / ``group_any_on``
    aggregates (the real ``Group`` walks the registry on access). Default
    uses the controller's loaded node registry.
    """
    return Group.from_record(
        record,
        controller._loaded.profile,
        controller._client,
        nodes=nodes if nodes is not None else controller._loaded.nodes,
    )


def make_program(record: ProgramRecord, controller: Controller) -> Program:
    return Program(record, controller._client)


def make_folder(record: FolderRecord) -> Folder:
    return Folder(record)


def make_network_resource(
    record: NetworkResourceRecord, controller: Controller
) -> NetworkResource:
    return NetworkResource(record, controller._client)


def make_variable(record: VariableRecord, controller: Controller) -> Variable:
    return Variable.from_record(record, controller._client)
