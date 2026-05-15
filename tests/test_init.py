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
    so HA can decide between retry vs. reauth (lines 92-108)."""
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
    on shutdown (lines 150-154)."""
    from pyisyox import Controller

    with patch.object(Controller, "stop", new=AsyncMock(return_value=None)) as stop:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        stop.assert_awaited()


async def test_async_remove_config_entry_device_blocks_known_devices(
    hass: HomeAssistant,
) -> None:
    """Returns ``False`` for a device whose identifier is still owned by
    the entry, ``True`` otherwise (line 229)."""
    from unittest.mock import MagicMock

    from custom_components.udi_iox import async_remove_config_entry_device

    entry = MagicMock()
    entry.runtime_data.devices = {"node_a", "node_b"}
    known_device = MagicMock()
    known_device.identifiers = {(DOMAIN, "node_a")}
    unknown_device = MagicMock()
    unknown_device.identifiers = {(DOMAIN, "extinct_addr")}

    assert await async_remove_config_entry_device(hass, entry, known_device) is False
    assert await async_remove_config_entry_device(hass, entry, unknown_device) is True
