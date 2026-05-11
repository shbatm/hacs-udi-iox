"""Tests for the udi_iox system_health surface.

Pins:
- host reachability + device_connected baseline rows still fire.
- When ``controller.websocket`` is populated, the new
  ``event_stream_status`` + ``last_event_at`` rows surface its
  state and last-frame timestamp.
- When ``controller.websocket`` is None (start_websocket=False
  one-shot loads, smoke fixtures), those rows are omitted rather
  than reported as falsy — otherwise the page would render
  "disconnected" even though the integration is happy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.udi_iox.models import IsyData
from custom_components.udi_iox.system_health import system_health_info
from tests.builders import make_controller, make_load_result


async def _wire_entry_with_controller(
    hass, controller, *, host: str = "http://eisy.local"
) -> None:
    """Patch the entries lookup so system_health_info finds our stub."""
    isy_data = IsyData()
    isy_data.root = controller

    entry = MagicMock()
    entry.runtime_data = isy_data
    entry.data = {"host": host}
    hass.config_entries.async_entries = MagicMock(return_value=[entry])


async def test_system_health_includes_ws_rows_when_websocket_present(hass, monkeypatch):
    """When the controller exposes a live WS, ``event_stream_status`` +
    ``last_event_at`` show up alongside the baseline rows."""
    controller = make_controller(make_load_result())
    captured_at = datetime(2026, 5, 10, 14, 35, tzinfo=UTC)
    # ``_ws`` is the backing slot for the ``websocket`` property.
    controller._ws = SimpleNamespace(
        status=SimpleNamespace(value="connected"),
        last_event_at=captured_at,
    )

    monkeypatch.setattr(
        "custom_components.udi_iox.system_health.system_health.async_check_can_reach_url",
        AsyncMock(return_value=True),
    )
    await _wire_entry_with_controller(hass, controller)

    info = await system_health_info(hass)

    assert info["host_reachable"] is True
    assert info["device_connected"] is True
    assert info["event_stream_status"] == "connected"
    assert info["last_event_at"] == captured_at


async def test_system_health_omits_ws_rows_when_websocket_is_none(hass, monkeypatch):
    """Without a WS (start_websocket=False), the ws rows are omitted
    so the page doesn't misrepresent a happy integration as broken."""
    controller = make_controller(make_load_result())
    # ``_ws`` defaults to None via the Controller __init__ — leave it.

    monkeypatch.setattr(
        "custom_components.udi_iox.system_health.system_health.async_check_can_reach_url",
        AsyncMock(return_value=True),
    )
    await _wire_entry_with_controller(hass, controller)

    info = await system_health_info(hass)

    assert "event_stream_status" not in info
    assert "last_event_at" not in info
    # Baseline rows still report.
    assert info["host_reachable"] is True
    assert info["device_connected"] is True


async def test_system_health_returns_empty_when_no_entries(hass):
    """No config entries → empty dict, no crashes."""
    hass.config_entries.async_entries = MagicMock(return_value=[])
    info = await system_health_info(hass)
    assert info == {}
