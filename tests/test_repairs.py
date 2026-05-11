"""Tests for the udi_iox repair-flow factory.

Exercises ``async_create_fix_flow`` directly — pinning that it
hands back the right :class:`RepairsFlow` for our one issue id, and
that an unknown id raises. The end-to-end HTTP repair flow is
covered by HA Core's repair-platform fixtures and is too heavy for
this fork's test surface.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.udi_iox.repairs import (
    LifecycleReloadRepairFlow,
    async_create_fix_flow,
)


@pytest.fixture
def fake_entry():
    entry = MagicMock()
    entry.entry_id = "abc-entry"
    entry.title = "eisy (portal)"
    return entry


async def test_create_fix_flow_returns_lifecycle_flow_for_matching_id(
    hass: HomeAssistant, fake_entry
) -> None:
    hass.config_entries.async_get_entry = MagicMock(return_value=fake_entry)

    flow = await async_create_fix_flow(
        hass,
        "lifecycle_reload_required.abc-entry",
        {"entry_id": "abc-entry"},
    )

    assert isinstance(flow, LifecycleReloadRepairFlow)
    assert flow.entry is fake_entry


async def test_create_fix_flow_rejects_unknown_issue_id(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError, match="unknown repair"):
        await async_create_fix_flow(
            hass, "some_other_issue.foo", {"entry_id": "abc-entry"}
        )


async def test_create_fix_flow_rejects_missing_entry_data(hass: HomeAssistant) -> None:
    """No data → can't resolve entry → don't pretend we have a flow."""
    with pytest.raises(ValueError):
        await async_create_fix_flow(hass, "lifecycle_reload_required.abc-entry", None)


async def test_confirm_step_reloads_entry(hass: HomeAssistant, fake_entry) -> None:
    """Submitting the confirm form triggers
    ``hass.config_entries.async_reload(entry_id)`` and closes the flow."""
    hass.config_entries.async_reload = AsyncMock()
    flow = LifecycleReloadRepairFlow(fake_entry)
    flow.hass = hass

    # First call (no user_input) shows the form.
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "confirm"

    # Submitting reloads + closes.
    result = await flow.async_step_confirm({})
    hass.config_entries.async_reload.assert_awaited_once_with("abc-entry")
    assert result["type"] == "create_entry"
