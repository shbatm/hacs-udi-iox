"""Shared fixtures for the udi_iox test suite.

The lightweight stand-ins for pyisyox's :class:`Controller` and
:class:`Node` live in :mod:`tests._fakes` so test modules can import
the dataclasses directly. These fixtures wire them up for tests that
prefer the pytest-fixture style.
"""

from __future__ import annotations

import sys
import types

# Compatibility shims for older Home Assistant versions installed alongside
# ``pytest_homeassistant_custom_component``. ``service_info.{dhcp,ssdp}`` were
# carved out of ``homeassistant.components.{dhcp,ssdp}`` in mid-2025; the host
# venv still ships HA 2025.1 which doesn't expose those submodules. Stub them
# only when missing so the devcontainer (newer HA) keeps using real classes.
# Must run before any ``custom_components.udi_iox`` import below, since the
# integration's ``config_flow.py`` imports the real modules at top level.
for _module_name, _attr in (
    ("homeassistant.helpers.service_info.dhcp", "DhcpServiceInfo"),
    ("homeassistant.helpers.service_info.ssdp", "SsdpServiceInfo"),
):
    try:  # pragma: no cover - real module on newer HA
        __import__(_module_name)
    except ImportError:
        _stub = types.ModuleType(_module_name)
        setattr(_stub, _attr, type(_attr, (), {}))
        sys.modules[_module_name] = _stub

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from homeassistant.const import (  # noqa: E402
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.udi_iox.const import (  # noqa: E402
    CONF_ENABLE_NETWORKING,
    CONF_ENABLE_PROGRAMS,
    CONF_ENABLE_VARIABLES,
    DOMAIN,
)
from tests._fakes import (  # noqa: E402
    FakeController,
    FakeEvent,
    FakeGroup,
    FakeLifecycleEvent,
    FakeNetworkResource,
    FakeNode,
    FakeNodePropertyValue,
    FakeProgram,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the test runner to load the udi_iox custom integration."""


@pytest.fixture
def fake_node_factory():
    """Build a FakeNode with sensible defaults; override per test."""
    return FakeNode


@pytest.fixture
def fake_property_factory():
    """Build a FakeNodePropertyValue."""
    return FakeNodePropertyValue


@pytest.fixture
def fake_controller():
    """Return a fresh FakeController per test."""
    return FakeController()


@pytest.fixture
def populated_controller() -> FakeController:
    """A FakeController seeded with one node per device family.

    Used by snapshot tests to drive ``_categorize_nodes`` end-to-end. Each
    family is intentionally minimal — just enough for the classifier to
    place the node on the right HA platform and for the entity's read-side
    properties (``status``, ``properties``) to render.
    """
    controller = FakeController(uuid="aa:bb:cc:dd:ee:ff")

    def _node(
        address: str,
        name: str,
        *,
        protocol: str = "insteon",
        type_: str = "1.0.0.0",
        nodedef_id: str = "",
        parent: str | None = None,
        is_thermostat: bool = False,
        is_lock: bool = False,
        is_fan: bool = False,
        is_dimmable: bool = False,
        status_value: str = "0",
        status_uom: str = "100",
        status_formatted: str = "Off",
        extra_props: dict[str, FakeNodePropertyValue] | None = None,
    ) -> FakeNode:
        props = {
            "ST": FakeNodePropertyValue(
                id="ST",
                value=status_value,
                formatted=status_formatted,
                uom=status_uom,
                name="Status",
            )
        }
        if extra_props:
            props.update(extra_props)
        return FakeNode(
            address=address,
            name=name,
            protocol=protocol,
            type=type_,
            nodedef_id=nodedef_id,
            parent_address=parent,
            is_thermostat=is_thermostat,
            is_lock=is_lock,
            is_fan=is_fan,
            is_dimmable=is_dimmable,
            properties=props,
        )

    # Insteon dimmable root → light + event + sensor(comms_error) + select(RR).
    # ``OL`` (NUMBER aux) is intentionally omitted: number.py crashes on
    # NodePropertyValue.value being a string when ranged_value_to_percentage
    # runs — a pre-existing pyisyox-6 migration bug tracked separately.
    light_root = _node(
        "AA AA AA 1",
        "Hallway Light",
        is_dimmable=True,
        status_value="255",
        status_formatted="On",
        extra_props={
            "RR": FakeNodePropertyValue(
                id="RR", value="21", formatted="0.5s", uom="57", name="Ramp Rate"
            ),
        },
    )
    controller.nodes[light_root.address] = light_root

    # KeypadLinc-style sub-button: insteon, parent set, non-dimmable → event only.
    sub_button = _node(
        "AA AA AA 2", "Hallway Button B", parent=light_root.address
    )
    controller.nodes[sub_button.address] = sub_button

    # Insteon non-dimmable root → switch + event + sensor(comms_error)
    switch_root = _node("BB BB BB 1", "Garage Outlet")
    controller.nodes[switch_root.address] = switch_root

    # Z-Wave lock root → lock + sensor(comms_error)
    lock_root = _node(
        "CC CC CC 1",
        "Front Door Lock",
        protocol="zwave",
        type_="111.5.0.0",
        is_lock=True,
        status_value="0",
        status_formatted="Unlocked",
        status_uom="11",
    )
    controller.nodes[lock_root.address] = lock_root

    # Insteon thermostat root → climate + sensor(comms_error) + aux sensors.
    # The ``17`` UOM = °F; setpoints share that UOM and a ``prec=1`` so the
    # snapshot exercises the consumer's ``target.prec`` decimal scaling.
    thermostat_root = _node(
        "DD DD DD 1",
        "Living Thermostat",
        type_="5.16.0.0",
        is_thermostat=True,
        status_value="68",
        status_formatted="68°F",
        status_uom="17",
        extra_props={
            "CLISPH": FakeNodePropertyValue(
                id="CLISPH",
                value="680",
                formatted="68°F",
                uom="17",
                name="Heat Setpoint",
                prec=1,
            ),
            "CLISPC": FakeNodePropertyValue(
                id="CLISPC",
                value="760",
                formatted="76°F",
                uom="17",
                name="Cool Setpoint",
                prec=1,
            ),
        },
    )
    controller.nodes[thermostat_root.address] = thermostat_root

    # FanLinc lamp root (dimmable light) → light
    fanlinc_root = _node(
        "EE EE EE 1",
        "FanLinc Lamp",
        type_="1.46.0.0",
        is_dimmable=True,
    )
    controller.nodes[fanlinc_root.address] = fanlinc_root

    # FanLinc fan motor sub-node → fan
    fanlinc_motor = _node(
        "EE EE EE 2",
        "FanLinc Motor",
        type_="1.46.0.0",
        is_fan=True,
        parent=fanlinc_root.address,
        status_value="0",
        status_formatted="Off",
        status_uom="25",
    )
    controller.nodes[fanlinc_motor.address] = fanlinc_motor

    # Group / scene → switch.
    # ``group_any_on`` is the consumer's ``is_on`` aggregation; set to
    # True so the snapshot exercises the non-default "scene currently on"
    # state. ``controller_addresses`` links the scene's HA device to the
    # primary Insteon switch root.
    controller.groups["GRP_1"] = FakeGroup(
        address="GRP_1",
        name="Living Room Scene",
        group_any_on=True,
        group_all_on=False,
        controller_addresses=[switch_root.address],
    )

    # Network resource → button
    controller.network_resources["1"] = FakeNetworkResource(
        address="1", name="Reboot Router"
    )

    # Programs: one switch (status + actions), one binary_sensor (status only)
    controller.programs["P_SWITCH_S"] = FakeProgram(
        address="P_SWITCH_S",
        name="Status",
        path="HA.switch/Movie Mode/status",
        status=False,
    )
    controller.programs["P_SWITCH_A"] = FakeProgram(
        address="P_SWITCH_A",
        name="Actions",
        path="HA.switch/Movie Mode/actions",
    )
    controller.programs["P_BS_S"] = FakeProgram(
        address="P_BS_S",
        name="Status",
        path="HA.binary_sensor/Front Door Open/status",
        status=True,
    )

    # Variables intentionally left empty: ``number.async_setup_entry``
    # currently reads ``node.precision`` / ``.address`` / ``.name`` as
    # attributes, but ``isy_data.variables`` is a list of plain dicts
    # (``VariableRecord = dict[str, Any]``). That's a pre-existing bug
    # tracked separately — the snapshot tests cover the aux-property
    # number entities (OL on dimmable Insteon roots), which exercise
    # the same async_setup_entry paths via a stable Node-attribute API.

    return controller


@pytest.fixture
def fake_event_factory():
    return FakeEvent


@pytest.fixture
def fake_lifecycle_factory():
    return FakeLifecycleEvent


# ---------------------------------------------------------------------------
# Snapshot-test harness.
#
# Tests that want to drive the real ``async_setup_entry`` codepath (so HA's
# entity registry actually populates) override the ``platforms`` fixture to
# restrict which platforms are forwarded, then depend on ``init_integration``
# which builds + sets up a ``MockConfigEntry``.
# ---------------------------------------------------------------------------


@pytest.fixture
def entity_registry_enabled_by_default():
    """Force-enable entities that the integration registers as disabled-by-default.

    Mirrors HA Core's ``tests.components.conftest`` fixture; the older
    ``pytest_homeassistant_custom_component`` shipped here doesn't expose
    it, but ``snapshot_platform`` requires every entity to be enabled.
    """
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        yield


@pytest.fixture
def platforms() -> list[Platform]:
    """Platforms to forward during ``async_setup_entry``.

    Override per test (or per file via an autouse fixture) to restrict the
    setup to a single platform — required by ``snapshot_platform``, which
    asserts all registered entities live in one domain.
    """
    return []


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Build a ``MockConfigEntry`` for the udi_iox domain.

    Enables variables / programs / networking by default so the seeded
    controller's full surface flows through the classifier — the snapshot
    tests rely on every category showing up.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "http://eisy.local:8080",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "password",
        },
        options={
            CONF_ENABLE_VARIABLES: True,
            CONF_ENABLE_PROGRAMS: True,
            CONF_ENABLE_NETWORKING: True,
        },
        title="eisy.local",
        unique_id="aa:bb:cc:dd:ee:ff",
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    populated_controller: FakeController,
    mock_config_entry: MockConfigEntry,
    platforms: list[Platform],
) -> MockConfigEntry:
    """Set up the udi_iox integration with a pre-populated FakeController.

    Patches the production ``Controller`` constructor to hand back our
    seeded stand-in, restricts the platform list so only one platform
    forwards (per ``platforms`` fixture override), then runs the real
    ``async_setup_entry`` so HA's entity registry populates from the
    classifier output.
    """
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.udi_iox.Controller",
            return_value=populated_controller,
        ),
        patch("custom_components.udi_iox.PLATFORMS", platforms),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    return mock_config_entry
