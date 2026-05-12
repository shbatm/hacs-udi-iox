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
from pyisyox.schema.nodedef import NodeDef
from pyisyox.schema.profile import Family, Instance

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
    status_precision: int = 0,
) -> NodeRecord:
    """Build a minimal :class:`NodeRecord`.

    ``status_*`` kwargs are a shortcut for the always-present ``ST``
    property. Override ``properties`` to take full control (e.g. plugin
    nodes that don't carry a status, or thermostat setpoint properties).

    ``pnode`` defaults to the **node's own address** when not supplied —
    that's the wire convention for Insteon device roots (the primary is
    the device itself). For sub-buttons of multi-button physicals
    (KeypadLinc, RemoteLinc, FanLinc), pass ``pnode=<primary_address>``
    explicitly. ``parent_address`` is the tree-hierarchy parent (folder
    containing the node) and is independent — leave it ``None`` unless
    you're specifically testing folder/tree behavior.
    """
    if properties is None:
        properties = {
            "ST": NodePropertyValue(
                id="ST",
                value=status_value,
                formatted=status_formatted,
                uom=status_uom,
                name="Status",
                precision=status_precision,
            ),
        }
    # Native Insteon nodes carry an ERR (comms-error counter) property on
    # the wire — the integration surfaces it as the diagnostic
    # ``device_communication_errors`` ("…responding") sensor. Seed it for
    # any family-1 record (default ST-only AND callers that supply
    # ``properties=``) so the diagnostic appears on every Insteon
    # fixture. Z-Wave (family "4") / plugin (family "100"+) nodes don't
    # carry ERR and intentionally skip this.
    if family_id == "1" and "ERR" not in properties:
        properties["ERR"] = NodePropertyValue(
            id="ERR",
            value="0",
            formatted="0",
            uom="0",
            name="Responding",
            precision=0,
        )
    return NodeRecord(
        address=address,
        name=name,
        nodedef_id=nodedef_id,
        family_id=family_id,
        instance_id=instance_id,
        type=type_,
        parent_address=parent_address,
        pnode=pnode or address,
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
    precision: int = 0,
    ts: str = "",
) -> VariableRecord:
    return VariableRecord(
        type_id=type_id,
        id=id_,
        name=name,
        value=value,
        init=init,
        precision=precision,
        ts=ts,
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
        "run_program_command",
        "run_network_resource",
    ):
        setattr(client, method_name, AsyncMock(return_value=None))

    return client


# ---------------------------------------------------------------------------
# Event firing helpers — drive listeners on a real Controller's dispatcher.
#
# The real ``EventDispatcher`` keeps three listener lists (events,
# lifecycle, program-status). Tests that want to assert on the
# consumer's dispatch logic synthesise ``Event`` / ``NodeLifecycleEvent``
# / ``ProgramStatusEvent`` instances and route them to the dispatcher's
# listeners via the helpers below. We hit the dispatcher's internal
# lists directly because the public ``feed`` path requires raw XML
# frames, which would force every test to round-trip its synthetic
# events through pyisyox's parser. The shape contract is locked by
# pyisyox's own test suite — these helpers just fan the dataclass out
# to whatever listeners the consumer registered.
# ---------------------------------------------------------------------------


def fire_event(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.Event`) to every event listener
    on ``controller``'s dispatcher."""
    for listener in tuple(controller._dispatcher._listeners):
        listener(event)


def fire_lifecycle(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.NodeLifecycleEvent`) to every
    lifecycle listener on ``controller``'s dispatcher."""
    for listener in tuple(controller._dispatcher._lifecycle_listeners):
        listener(event)


def fire_program_status(controller: Controller, event: Any) -> None:
    """Fan ``event`` (a :class:`pyisyox.runtime.events.ProgramStatusEvent`)
    to every program-status listener on ``controller``'s dispatcher."""
    for listener in tuple(controller._dispatcher._program_status_listeners):
        listener(event)


# ---------------------------------------------------------------------------
# Per-platform node shortcuts.
#
# Native introspection (``is_thermostat`` / ``is_lock`` / ``is_dimmable`` /
# ``is_fan``) is derived from the resolved nodedef + editor codec on the
# bundled profile. These shortcuts pin a nodedef id that produces the
# expected classification, so classifier unit tests don't need to know
# pyisyox's introspection internals.
# ---------------------------------------------------------------------------

#: Nodedef ids in ``tests/fixtures/eisy6_profile.json`` that classify cleanly
#: to each native platform. ``RelayLampSwitch_ADV`` is the non-dimmable
#: keypad sub-button shape the consumer's sub-button suppression rule
#: targets.
NODEDEF_FOR_PLATFORM: dict[str, str] = {
    "climate": "Thermostat",
    "lock": "DoorLock",
    "light": "DimmerLampOnly",
    "fan": "FanLincMotor",
    "switch": "RelayLampOnly",
    "subbutton": "RelayLampSwitch_ADV",
    "subdimmer": "DimmerLampSwitch_ADV",
}


# ---------------------------------------------------------------------------
# Plugin cover nodedef — synthetic, injected on demand.
#
# The bundled ``eisy6_profile.json`` is a real anonymized capture from a
# stock eisy 6.x; it carries no PG3 plugins. To exercise the cover
# platform path (``pyisyox.classify`` returning ``ControllablePlatform.COVER``
# when accepts has ``FDUP``/``FDDOWN``/``FDSTOP`` and no ``DON``/``DOF``),
# the cover snapshot test asks for a profile **derived** from the bundled
# one with a synthetic plugin family slot grafted in.
#
# The plugin slot id (``"100"``) deliberately stays outside the documented
# native family ids so ``Node.protocol`` returns ``"node_server"`` —
# which is the consumer's switch case for "defer to the pyisyox classifier"
# instead of "use native is_dimmable / is_lock / is_fan introspection".
# ---------------------------------------------------------------------------

PLUGIN_COVER_FAMILY_ID = "100"
PLUGIN_COVER_INSTANCE_ID = "1"
PLUGIN_COVER_NODEDEF_ID = "BlindShade"


def _build_plugin_cover_nodedef() -> NodeDef:
    """Construct a PG3-shape cover nodedef.

    Accepts ``FDUP`` / ``FDDOWN`` / ``FDSTOP`` (and ``QUERY``) but not
    ``DON`` / ``DOF``, so the classifier picks ``ControllablePlatform.COVER``
    rather than light / switch. One ``ST`` property using the standard
    on-level editor — enough surface for ``ISYCoverEntity._update_cover_attrs``
    to read a value off ``node.status``.
    """
    return NodeDef.from_json(
        {
            "id": PLUGIN_COVER_NODEDEF_ID,
            "nls": "blind",
            "properties": [
                {"id": "ST", "editor": "I_OL", "name": "Status"},
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "FDUP", "name": "Open"},
                    {"id": "FDDOWN", "name": "Close"},
                    {"id": "FDSTOP", "name": "Stop"},
                    {"id": "QUERY", "name": "Query"},
                ],
            },
        },
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
    )


def make_profile_with_cover_plugin() -> Profile:
    """Return a fresh :class:`Profile` (loaded from the bundled eisy6
    capture) with a synthetic PG3-shape cover nodedef injected under
    plugin slot ``"100"``.

    Built fresh per call — the LRU-cached :func:`load_profile` returns a
    shared instance, and we mustn't mutate it.
    """
    raw = json.loads((FIXTURE_DIR / "eisy6_profile.json").read_text())
    profile = Profile.load_from_json(raw)

    nodedef = _build_plugin_cover_nodedef()
    instance = Instance(id=PLUGIN_COVER_INSTANCE_ID, name="Blind Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    family = Family(id=PLUGIN_COVER_FAMILY_ID, name="Blind Plugin")
    family.instances[PLUGIN_COVER_INSTANCE_ID] = instance
    profile.families[PLUGIN_COVER_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_cover_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the cover-plugin-augmented profile.

    Use with a cover :class:`NodeRecord` built via
    :func:`make_plugin_cover_node_record` so the classifier resolves the
    nodedef and routes the node onto ``Platform.COVER``.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_cover_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_cover_node_record(
    address: str = "n100_blind1",
    name: str = "Living Room Blind",
    *,
    status_value: str = "0",
) -> NodeRecord:
    """Build a :class:`NodeRecord` shaped like a PG3 cover plugin's
    blind / shade — family slot ``"100"``, instance ``"1"``, nodedef
    ``BlindShade`` (matches :func:`_build_plugin_cover_nodedef`).
    """
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_COVER_NODEDEF_ID,
        family_id=PLUGIN_COVER_FAMILY_ID,
        instance_id=PLUGIN_COVER_INSTANCE_ID,
        type_="",
        status_value=status_value,
        status_uom="100",
        status_formatted="0%" if status_value == "0" else "Open",
    )


# --- plugin "hub" nodedef: no controllable, zero-arg accept verbs -----
#
# Models a PG3 controller-style node (Flume / Harmony hub shape): accepts
# a couple of zero-arg verbs (``DISCOVER`` parameterless, ``BEEP`` with
# one *optional* level param) plus the implicit ``QUERY``, and carries a
# status property. pyisyox's classifier returns no controllable, two
# ``buttons``, one reading — so the consumer surfaces a Query button
# (root scaffold) plus a Discover + Beep button.

PLUGIN_HUB_FAMILY_ID = "101"
PLUGIN_HUB_INSTANCE_ID = "1"
PLUGIN_HUB_NODEDEF_ID = "PluginHub"


def _build_plugin_hub_nodedef() -> NodeDef:
    """PG3-shape hub nodedef — no ``DON``/``DOF`` (no controllable
    platform), zero-arg accept verbs, one ``ST`` property."""
    return NodeDef.from_json(
        {
            "id": PLUGIN_HUB_NODEDEF_ID,
            "nls": "hub",
            "properties": [
                {"id": "ST", "editor": "I_OL", "name": "Status"},
            ],
            "cmds": {
                "sends": [],
                "accepts": [
                    {"id": "DISCOVER", "name": "Discover"},
                    {
                        "id": "BEEP",
                        "name": "Beep",
                        "parameters": [{"id": "", "editor": "I_OL", "optional": True}],
                    },
                    {"id": "QUERY", "name": "Query"},
                ],
            },
        },
        family_id=PLUGIN_HUB_FAMILY_ID,
        instance_id=PLUGIN_HUB_INSTANCE_ID,
    )


def make_profile_with_button_plugin() -> Profile:
    """Bundled eisy6 profile with the synthetic ``PluginHub`` nodedef
    grafted under plugin slot ``"101"``. Built fresh per call (the cached
    :func:`load_profile` instance must not be mutated)."""
    raw = json.loads((FIXTURE_DIR / "eisy6_profile.json").read_text())
    profile = Profile.load_from_json(raw)

    nodedef = _build_plugin_hub_nodedef()
    instance = Instance(id=PLUGIN_HUB_INSTANCE_ID, name="Hub Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    family = Family(id=PLUGIN_HUB_FAMILY_ID, name="Hub Plugin")
    family.instances[PLUGIN_HUB_INSTANCE_ID] = instance
    profile.families[PLUGIN_HUB_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_button_plugin_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the hub-plugin-augmented profile.

    Use with :func:`make_plugin_hub_node_record` so the classifier
    resolves the nodedef and fans its zero-arg accepts into
    ``aux_properties[Platform.BUTTON]``.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_button_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_hub_node_record(
    address: str = "n101_hub",
    name: str = "Plugin Hub",
    *,
    status_value: str = "0",
) -> NodeRecord:
    """A :class:`NodeRecord` shaped like a PG3 hub/controller node —
    family slot ``"101"``, instance ``"1"``, nodedef ``PluginHub``."""
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_HUB_NODEDEF_ID,
        family_id=PLUGIN_HUB_FAMILY_ID,
        instance_id=PLUGIN_HUB_INSTANCE_ID,
        type_="",
        status_value=status_value,
        status_uom="100",
        status_formatted="0%",
    )


# --- plugin "trigger source" nodedef: only cmds.sends, no controllable --
#
# Models a PG3 sensor/doorbell-style node that emits verbs but accepts
# none — pyisyox's classifier returns no controllable, no readings, and
# two ``triggers``. The consumer wires it onto Platform.EVENT with
# event_types derived from the sent commands' names.

PLUGIN_TRIGGER_FAMILY_ID = "102"
PLUGIN_TRIGGER_INSTANCE_ID = "1"
PLUGIN_TRIGGER_NODEDEF_ID = "PluginTriggerSource"


def _build_plugin_trigger_nodedef() -> NodeDef:
    """PG3-shape trigger-source nodedef — ``cmds.sends`` only, no accepts."""
    return NodeDef.from_json(
        {
            "id": PLUGIN_TRIGGER_NODEDEF_ID,
            "nls": "trigger",
            "properties": [],
            "cmds": {
                "sends": [
                    {"id": "DOORBELL_PRESS", "name": "Doorbell Press"},
                    {"id": "MOTION_ON", "name": "Motion On"},
                ],
                "accepts": [],
            },
        },
        family_id=PLUGIN_TRIGGER_FAMILY_ID,
        instance_id=PLUGIN_TRIGGER_INSTANCE_ID,
    )


def make_profile_with_trigger_plugin() -> Profile:
    """Bundled eisy6 profile with the synthetic ``PluginTriggerSource``
    nodedef grafted under plugin slot ``"102"``. Built fresh per call."""
    raw = json.loads((FIXTURE_DIR / "eisy6_profile.json").read_text())
    profile = Profile.load_from_json(raw)

    nodedef = _build_plugin_trigger_nodedef()
    instance = Instance(id=PLUGIN_TRIGGER_INSTANCE_ID, name="Trigger Plugin")
    instance.nodedefs[nodedef.id] = nodedef
    family = Family(id=PLUGIN_TRIGGER_FAMILY_ID, name="Trigger Plugin")
    family.instances[PLUGIN_TRIGGER_INSTANCE_ID] = instance
    profile.families[PLUGIN_TRIGGER_FAMILY_ID] = family
    profile.nodedef_lookup[nodedef.lookup_key] = nodedef
    return profile


def make_trigger_plugin_load_result(
    *,
    uuid: str = DEFAULT_UUID,
    version: str = "6.0.0a1",
    nodes: dict[str, NodeRecord] | None = None,
) -> LoadResult:
    """A :class:`LoadResult` carrying the trigger-plugin-augmented profile.

    Use with :func:`make_plugin_trigger_node_record` so the classifier
    resolves the nodedef and the consumer routes the node onto
    ``Platform.EVENT``.
    """
    return LoadResult(
        config=ControllerConfig(uuid=uuid, version=version),
        profile=make_profile_with_trigger_plugin(),
        nodes=nodes or {},
        groups={},
        folders={},
        programs={},
        triggers=[],
        variables={"1": {}, "2": {}},
        network_resources={},
    )


def make_plugin_trigger_node_record(
    address: str = "n102_bell",
    name: str = "Front Doorbell",
) -> NodeRecord:
    """A :class:`NodeRecord` shaped like a PG3 trigger-source node —
    family slot ``"102"``, instance ``"1"``, nodedef ``PluginTriggerSource``,
    no status property."""
    return make_node_record(
        address,
        name,
        nodedef_id=PLUGIN_TRIGGER_NODEDEF_ID,
        family_id=PLUGIN_TRIGGER_FAMILY_ID,
        instance_id=PLUGIN_TRIGGER_INSTANCE_ID,
        type_="",
        properties={},
    )


def make_classified_node_record(
    address: str,
    name: str,
    *,
    target: str,
    pnode: str | None = None,
    family_id: str = "1",
    properties: dict[str, NodePropertyValue] | None = None,
    **status_kwargs,
) -> NodeRecord:
    """Shortcut for :func:`make_node_record` that picks a real nodedef id
    for the requested target platform.

    ``target`` is one of the keys in :data:`NODEDEF_FOR_PLATFORM`. Lock
    uses ``family_id="4"`` (Z-Wave) by default; everything else is
    Insteon family ``"1"``. Override via the ``family_id`` kwarg.

    Pass ``pnode=<primary_address>`` for sub-buttons of multi-button
    devices (KeypadLinc, RemoteLinc, FanLinc).
    """
    if target == "lock":
        family_id = "4"
    return make_node_record(
        address,
        name,
        nodedef_id=NODEDEF_FOR_PLATFORM[target],
        family_id=family_id,
        pnode=pnode,
        properties=properties,
        **status_kwargs,
    )


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
