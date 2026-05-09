"""Provide info to system health."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from pyisyox import Controller

from .const import DOMAIN
from .models import IsyConfigEntry


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the system health page.

    pyisyox 6's ``Controller`` doesn't surface a public WebSocket
    handle, so the v3-era ``last_heartbeat`` / ``status`` rows are
    deferred until pyisyox exposes them. The two rows below are the
    ones we can produce reliably from the current public surface.
    """
    health_info: dict[str, Any] = {}

    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return health_info

    entry: IsyConfigEntry = entries[0]  # type: ignore[assignment]
    isy_data = entry.runtime_data
    controller: Controller = isy_data.root

    health_info["host_reachable"] = await system_health.async_check_can_reach_url(
        hass, entry.data[CONF_HOST]
    )
    health_info["device_connected"] = controller.connected

    return health_info
