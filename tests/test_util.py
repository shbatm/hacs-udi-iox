"""Direct unit tests for the udi_iox util module."""

from __future__ import annotations

from unittest.mock import MagicMock

import homeassistant.helpers.entity_registry as er
import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.udi_iox.const import DOMAIN
from custom_components.udi_iox.util import _async_cleanup_registry_entries


def _make_entry_with(unique_ids: set[tuple[Platform, str]]) -> MagicMock:
    """Build a MagicMock IsyConfigEntry exposing the given unique_ids."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.runtime_data.unique_ids = unique_ids
    return entry


@pytest.fixture
def populated_registry(hass: HomeAssistant) -> er.EntityRegistry:
    """Pre-populate the entity registry with two switch entities for our entry."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        Platform.SWITCH,
        DOMAIN,
        "uid_keep",
        config_entry=_fake_entry(hass, "test_entry"),
    )
    registry.async_get_or_create(
        Platform.SWITCH,
        DOMAIN,
        "uid_remove",
        config_entry=_fake_entry(hass, "test_entry"),
    )
    return registry


def _fake_entry(hass: HomeAssistant, entry_id: str):
    """Register a real ConfigEntry so the registry accepts our entries."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id)
    entry.add_to_hass(hass)
    return entry


async def test_cleanup_removes_unique_ids_not_in_isy_data(
    hass: HomeAssistant, populated_registry: er.EntityRegistry
) -> None:
    """Entities whose unique_id isn't in ``isy_data.unique_ids`` are removed."""
    entry = _make_entry_with({(Platform.SWITCH, "uid_keep")})
    entry.entry_id = "test_entry"
    _async_cleanup_registry_entries(hass, entry)
    remaining = er.async_entries_for_config_entry(populated_registry, "test_entry")
    assert {e.unique_id for e in remaining} == {"uid_keep"}


async def test_cleanup_no_extras_is_a_noop(
    hass: HomeAssistant, populated_registry: er.EntityRegistry
) -> None:
    """When every registered entity is still owned, the function returns
    without touching the registry."""
    entry = _make_entry_with(
        {(Platform.SWITCH, "uid_keep"), (Platform.SWITCH, "uid_remove")}
    )
    entry.entry_id = "test_entry"
    before = len(er.async_entries_for_config_entry(populated_registry, "test_entry"))
    _async_cleanup_registry_entries(hass, entry)
    after = len(er.async_entries_for_config_entry(populated_registry, "test_entry"))
    assert before == after == 2
