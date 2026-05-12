"""Snapshot test for plugin-defined accept-command buttons.

PG3 controller / hub nodes (Flume, Harmony, ...) expose zero-arg accept
verbs (``DISCOVER``, ``BEEP``, ...) that pyisyox's classifier returns in
``ClassificationResult.buttons``. The consumer fans those into
``aux_properties[Platform.BUTTON]`` and ``button.py`` materialises one
``ISYNodeCommandButtonEntity`` each (``BEEP`` tagged ``identify``). The
bundled stock eisy6 profile carries no PG3 plugins, so this test grafts
a synthetic ``PluginHub`` nodedef in via ``tests.builders``.
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
    make_button_plugin_load_result,
    make_controller,
    make_plugin_hub_node_record,
)


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.BUTTON]


@pytest.fixture
def populated_controller():
    """Override the default fixture with a controller carrying the
    hub-plugin profile + a single hub node."""
    hub = make_plugin_hub_node_record()
    return make_controller(make_button_plugin_load_result(nodes={hub.address: hub}))


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_plugin_command_buttons(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every button entity created for a plugin hub node —
    the root-scaffold Query plus the classifier-derived Discover / Beep."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)
