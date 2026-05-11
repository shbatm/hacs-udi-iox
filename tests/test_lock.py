"""Snapshot tests for the udi_iox lock platform."""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
)


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.LOCK]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_lock_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every lock entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)
