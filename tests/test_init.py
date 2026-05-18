"""Direct tests for udi_iox setup-entry error paths and shutdown wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pyisyox import (
    ISYConnectionError,
    ISYInvalidAuthError,
)
from pyisyox.exceptions import ISYResponseParseError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.udi_iox import async_setup_entry
from custom_components.udi_iox.const import DOMAIN


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "http://eisy.local:8080",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "password",
        },
        title="eisy.local",
        unique_id="aa:bb:cc:dd:ee:ff",
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (TimeoutError("slow"), ConfigEntryNotReady),
        (ISYInvalidAuthError("nope"), ConfigEntryAuthFailed),
        (ISYConnectionError("down"), ConfigEntryNotReady),
        (ISYResponseParseError("garbage"), ConfigEntryNotReady),
    ],
)
async def test_async_setup_entry_translates_connect_errors(
    hass: HomeAssistant, raised: Exception, expected: type[Exception]
) -> None:
    """Each connect-time exception maps to the right HA exception type
    so HA can decide between retry vs. reauth."""
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.udi_iox.Controller.connect",
            new=AsyncMock(side_effect=raised),
        ),
        pytest.raises(expected),
    ):
        await async_setup_entry(hass, entry)


async def test_shutdown_handler_stops_controller(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The ``HOMEASSISTANT_STOP`` listener fires ``controller.stop()``
    on shutdown."""
    from pyisyox import Controller

    with patch.object(Controller, "stop", new=AsyncMock(return_value=None)) as stop:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        stop.assert_awaited()


async def test_async_remove_config_entry_device_blocks_known_devices(
    hass: HomeAssistant,
) -> None:
    """Returns ``False`` for a device whose identifier is still owned by
    the entry, ``True`` otherwise."""
    from unittest.mock import MagicMock

    from custom_components.udi_iox import async_remove_config_entry_device

    entry = MagicMock()
    # Real shape is dict[str, DeviceInfo]; the function only iterates
    # the keys, but match the production type so the test reads true.
    entry.runtime_data.devices = {"node_a": MagicMock(), "node_b": MagicMock()}
    known_device = MagicMock()
    known_device.identifiers = {(DOMAIN, "node_a")}
    unknown_device = MagicMock()
    unknown_device.identifiers = {(DOMAIN, "extinct_addr")}

    assert await async_remove_config_entry_device(hass, entry, known_device) is False
    assert await async_remove_config_entry_device(hass, entry, unknown_device) is True


async def test_enable_programs_false_gates_legacy_and_device_programs(
    hass: HomeAssistant,
    populated_controller,
) -> None:
    """CONF_ENABLE_PROGRAMS=False must gate BOTH the legacy
    ``HA.<platform>/`` virtual entities (``runtime_data.programs``) AND
    the program-as-device fan-out (``runtime_data.program_devices``).

    ``populated_controller`` seeds both kinds — ``HA.switch/Movie Mode``
    (legacy) and ``Lighting/Sunset Lights`` (device fan-out) — so an
    all-empty assertion is non-vacuous.
    """
    from custom_components.udi_iox.const import (
        CONF_ENABLE_NETWORKING,
        CONF_ENABLE_PROGRAMS,
        CONF_ENABLE_VARIABLES,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "http://eisy.local:8080",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "password",
        },
        options={
            CONF_ENABLE_PROGRAMS: False,
            CONF_ENABLE_VARIABLES: True,
            CONF_ENABLE_NETWORKING: True,
        },
        title="eisy.local",
        unique_id="aa:bb:cc:dd:ee:ff",
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.udi_iox.Controller",
            return_value=populated_controller,
        ),
        patch("pyisyox.Controller.connect", new=AsyncMock(return_value=None)),
        patch("pyisyox.Controller.stop", new=AsyncMock(return_value=None)),
        patch("custom_components.udi_iox.PLATFORMS", []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    data = entry.runtime_data
    assert data.program_devices == []
    assert all(progs == [] for progs in data.programs.values())
