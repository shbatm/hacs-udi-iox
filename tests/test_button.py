"""Snapshot tests for the udi_iox button platform."""

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
    return [Platform.BUTTON]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_button_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every button entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


def test_command_button_disabled_by_default_for_maintenance_verbs() -> None:
    """``WDU`` ("Write Changes") buttons are created disabled by default —
    a low-traffic maintenance verb — while everyday verbs are enabled."""
    from custom_components.udi_iox.button import ISYNodeCommandButtonEntity
    from custom_components.udi_iox.models import IsyData
    from tests.builders import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = IsyData()

    def _button(command_id: str) -> ISYNodeCommandButtonEntity:
        return ISYNodeCommandButtonEntity(
            isy_data,
            node,
            command_id=command_id,
            name=command_id.title(),
            unique_id=f"x_{command_id}",
            device_info=None,  # type: ignore[arg-type]
        )

    assert _button("WDU").entity_registry_enabled_default is False
    assert _button("DISCOVER").entity_registry_enabled_default is True
