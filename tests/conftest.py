"""Shared fixtures for the udi_iox test suite.

Tests drive a real :class:`pyisyox.Controller` (with its HTTP-side
client coroutines stubbed) via the factories in :mod:`pyisyox.testing`.
The fixtures here wire that controller up against an HA-side
``MockConfigEntry`` and forward only the platforms a given test wants
to exercise.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custom_components.udi_iox.models import IsyData

# Compatibility shims for older Home Assistant versions installed alongside
# ``pytest_homeassistant_custom_component``. ``service_info.{dhcp,ssdp}`` were
# carved out of ``homeassistant.components.{dhcp,ssdp}`` in mid-2025; HA
# releases that predate the split don't expose those submodules. Stub them
# only when missing so installs that already ship the real modules keep
# using them. Must run before any ``custom_components.udi_iox`` import below,
# since the integration's ``config_flow.py`` imports the real modules at
# top level.
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

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from homeassistant.const import (  # noqa: E402
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from pyisyox.client import NodePropertyValue  # noqa: E402
from pyisyox.testing import (  # noqa: E402
    make_controller,
    make_group_record,
    make_load_result,
    make_network_resource_record,
    make_node_record,
    make_program_record,
    make_variable_record,
)
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.udi_iox.const import (  # noqa: E402
    CONF_ENABLE_NETWORKING,
    CONF_ENABLE_PROGRAMS,
    CONF_ENABLE_VARIABLES,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the test runner to load the udi_iox custom integration."""


@pytest.fixture
def populated_controller():
    """A real :class:`pyisyox.Controller` seeded with one record per device family.

    Drives the snapshot tests through actual ``async_setup_entry`` — so the
    consumer's reads exercise the real ``Node`` / ``Group`` / ``Program`` /
    ``Variable`` attribute surface, with introspection (``is_thermostat`` /
    ``is_lock`` / ``is_dimmable`` / ``is_fan``) resolved against the bundled
    anonymized eisy6 profile.

    Each family is minimal: one record per platform the classifier cares
    about. Specific nodedef ids picked so the real profile resolves the
    right editor codecs:

    * ``DimmerLampOnly`` — dimmable Insteon → ``Platform.LIGHT``
    * ``RelayLampOnly`` — non-dimmable Insteon → ``Platform.SWITCH``
    * ``DoorLock`` — Z-Wave lock → ``Platform.LOCK``
    * ``Thermostat`` — Insteon climate → ``Platform.CLIMATE``
    * ``FanLincMotor`` — FanLinc fan side → ``Platform.FAN``
    * ``KeypadDimmer_ADV`` for the keypad sub-button (event-only).
    """
    # --- Nodes ----------------------------------------------------------
    light_root = make_node_record(
        "AA AA AA 1",
        "Hallway Light",
        nodedef_id="DimmerLampOnly",
        status_value="255",
        status_formatted="On",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="255", formatted="On", uom="100", name="Status"
            ),
            "RR": NodePropertyValue(
                id="RR", value="21", formatted="0.5 seconds", uom="25", name="Ramp Rate"
            ),
        },
    )
    sub_button = make_node_record(
        "AA AA AA 2",
        "Hallway Button B",
        # ``RelayLampSwitch_ADV`` is a non-dimmable keypad sub-button —
        # primary classifies to SWITCH; the consumer's
        # ``_categorize_nodes`` then suppresses it as a KeypadLinc-style
        # sub-button and routes it to EVENT instead.
        nodedef_id="RelayLampSwitch_ADV",
        pnode=light_root.address,
    )
    switch_root = make_node_record(
        "BB BB BB 1",
        "Garage Outlet",
        nodedef_id="RelayLampOnly",
    )
    lock_root = make_node_record(
        "CC CC CC 1",
        "Front Door Lock",
        nodedef_id="DoorLock",
        family_id="4",  # Z-Wave family
        type_="111.5.0.0",
        status_value="0",
        status_uom="11",
        status_formatted="Unlocked",
    )
    thermostat_root = make_node_record(
        "DD DD DD 1",
        "Living Thermostat",
        nodedef_id="Thermostat",
        type_="5.16.0.0",
        status_value="68",
        status_uom="17",
        status_formatted="68°F",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="68", formatted="68°F", uom="17", name="Status"
            ),
            "CLISPH": NodePropertyValue(
                id="CLISPH",
                value="680",
                formatted="68°F",
                uom="17",
                name="Heat Setpoint",
                precision=1,
            ),
            "CLISPC": NodePropertyValue(
                id="CLISPC",
                value="760",
                formatted="76°F",
                uom="17",
                name="Cool Setpoint",
                precision=1,
            ),
        },
    )
    fanlinc_root = make_node_record(
        "EE EE EE 1",
        "FanLinc Lamp",
        nodedef_id="DimmerLampOnly",
        type_="1.46.0.0",
    )
    fanlinc_motor = make_node_record(
        "EE EE EE 2",
        "FanLinc Motor",
        nodedef_id="FanLincMotor",
        type_="1.46.0.0",
        pnode=fanlinc_root.address,
        status_uom="25",
    )
    nodes = {
        record.address: record
        for record in (
            light_root,
            sub_button,
            switch_root,
            lock_root,
            thermostat_root,
            fanlinc_root,
            fanlinc_motor,
        )
    }

    # --- Groups / scenes ------------------------------------------------
    # `group_any_on` is computed from member ST values at access time —
    # switch_root has ST="0" so any-on is False here. Override by setting
    # the ST value on the member node directly in tests that need it.
    scene = make_group_record(
        "55090",
        "Living Room Scene",
        member_addresses=(switch_root.address,),
        controller_addresses=(switch_root.address,),
    )

    # --- Programs -------------------------------------------------------
    switch_status = make_program_record(
        "0001",
        "Status",
        path="HA.switch/Movie Mode/status",
        status=False,
    )
    switch_actions = make_program_record(
        "0002",
        "Actions",
        path="HA.switch/Movie Mode/actions",
    )
    binary_status = make_program_record(
        "0003",
        "Status",
        path="HA.binary_sensor/Front Door Open/status",
        status=True,
    )
    # A program *outside* the legacy HA.<platform>/<name>/{status,actions}
    # folder convention — used to exercise the per-program-device fan-out
    # (binary sensor + four sensors + two switches + five buttons).
    # ``make_program_record`` doesn't yet expose the rich runtime fields
    # (running / run_at_startup / *_time); set them via ``replace`` here
    # until the upstream helper grows kwargs for them.
    from dataclasses import replace as _dc_replace

    sunset_lights = _dc_replace(
        make_program_record(
            "0010",
            "Sunset Lights",
            path="Lighting/Sunset Lights",
            status=True,
            enabled=True,
        ),
        run_at_startup=False,
        running="idle",
        last_run_time="2026-05-13T18:42:11.000Z",
        last_finish_time="2026-05-13T18:42:13.000Z",
        next_scheduled_run_time="2026-05-14T18:42:00.000Z",
    )
    programs = {
        record.address: record
        for record in (switch_status, switch_actions, binary_status, sunset_lights)
    }

    # --- Network resources ---------------------------------------------
    network_resources = {
        "1": make_network_resource_record("1", "Reboot Router"),
    }

    # --- Variables ------------------------------------------------------
    # PR #68's typed Variable surface — the number platform reads value /
    # init / prec / name straight off the wrapper now.
    variables = {
        "1": {
            "10": make_variable_record(
                "1", "10", "Boost Mode", value=5, init=0, precision=0
            ),
        },
        "2": {},
    }

    load_result = make_load_result(
        nodes=nodes,
        groups={scene.address: scene},
        programs=programs,
        variables=variables,
        network_resources=network_resources,
    )
    return make_controller(load_result)


# ---------------------------------------------------------------------------
# Snapshot-test harness.
#
# Tests that want to drive the real ``async_setup_entry`` codepath (so HA's
# entity registry actually populates) override the ``platforms`` fixture to
# restrict which platforms are forwarded, then depend on ``init_integration``
# which builds + sets up a ``MockConfigEntry``.
# ---------------------------------------------------------------------------


def isy_data_for(controller) -> IsyData:
    """Build a minimal :class:`IsyData` carrier with ``root`` pinned.

    Direct-entity unit tests instantiate entities by hand (without
    going through ``async_setup_entry``) and only need an ``IsyData``
    that knows about its controller. Centralised here so test files
    don't each re-define an inline three-line helper.
    """
    from custom_components.udi_iox.models import IsyData

    data = IsyData()
    data.root = controller
    return data


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
    populated_controller,
    mock_config_entry: MockConfigEntry,
    platforms: list[Platform],
) -> MockConfigEntry:
    """Set up the udi_iox integration against the pre-populated real Controller.

    Patches the production ``Controller`` constructor to hand back our
    pre-loaded instance and stubs ``connect()`` / ``stop()`` so the
    network is never touched. Restricts the platform list so only one
    platform forwards (per ``platforms`` fixture override), then runs
    the real ``async_setup_entry`` so HA's entity registry populates
    from the classifier output against real types.
    """
    mock_config_entry.add_to_hass(hass)

    # ``connect()`` would clobber ``_loaded`` by hitting HTTP; patch it
    # (and ``stop``) at the class level since :class:`Controller` uses
    # ``__slots__``. The controller instance is already loaded.
    with (
        patch(
            "custom_components.udi_iox.Controller",
            return_value=populated_controller,
        ),
        patch(
            "pyisyox.Controller.connect",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pyisyox.Controller.stop",
            new=AsyncMock(return_value=None),
        ),
        patch("custom_components.udi_iox.PLATFORMS", platforms),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    return mock_config_entry
