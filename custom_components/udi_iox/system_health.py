"""Provide info to system health."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from pyisyox import Controller

from .const import DOMAIN, ISY_URL_POSTFIX
from .models import IsyConfigEntry


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the system health page.

    Surfaces four signals:

    * ``host_reachable`` — HTTP reachability of the controller's
      ``/desc`` endpoint (the lightest endpoint we know works on
      both ISY-994 and IoX 6).
    * ``device_connected`` — whether ``Controller.connect()``
      completed and ``stop()`` hasn't run. Tracks the initial
      handshake, not the live WS.
    * ``event_stream_status`` — most-recent :class:`EventStreamStatus`
      value from the WS reader (``"connected"`` / ``"lost_connection"``
      / ``"reconnecting"`` / ``"not_started"`` / ...). Read directly
      via ``controller.websocket.status`` — no listener subscription
      needed.
    * ``last_event_at`` — UTC datetime of the most recent text
      frame. The eisy emits a heartbeat every 30 s, so a stale
      value (>60 s) indicates the WS has silently stopped.
    """
    health_info: dict[str, Any] = {}

    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return health_info

    entry: IsyConfigEntry = entries[0]  # type: ignore[assignment]
    isy_data = entry.runtime_data
    controller: Controller = isy_data.root

    health_info["host_reachable"] = await system_health.async_check_can_reach_url(
        hass, f"{entry.data[CONF_HOST]}{ISY_URL_POSTFIX}"
    )
    health_info["device_connected"] = controller.connected

    ws = controller.websocket
    if ws is not None:
        health_info["event_stream_status"] = ws.status.value
        health_info["last_event_at"] = ws.last_event_at

    return health_info
