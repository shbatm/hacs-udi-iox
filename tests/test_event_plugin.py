"""Snapshot test for EVENT entities derived from a PG3 plugin's cmds.sends.

The bundled ``eisy6_profile.json`` is a stock-eisy capture with no PG3
plugins, so this fixture grafts a synthetic ``PluginTriggerSource``
nodedef (``cmds.sends = [DOORBELL_PRESS, MOTION_ON]``, no accepts) at
runtime via ``tests.builders.make_trigger_plugin_load_result``.

Pins: a plugin node with sent verbs but no controllable platform flows
through ``pyisyox.classify`` → ``ClassificationResult.triggers`` →
``Platform.EVENT``, and the event entity exposes the plugin verbs as
``event_types`` (lowercased command names) with no device class.
"""

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

from tests.builders import (
    make_controller,
    make_plugin_trigger_node_record,
    make_trigger_plugin_load_result,
)


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.EVENT]


@pytest.fixture
def populated_controller():
    """Override the default fixture with a controller that carries the
    trigger-plugin profile + a single trigger-source node."""
    bell = make_plugin_trigger_node_record()
    return make_controller(make_trigger_plugin_load_result(nodes={bell.address: bell}))


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_event_plugin_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot the event entity created for a plugin trigger source."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)
