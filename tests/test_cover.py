"""Snapshot tests for the udi_iox cover platform.

Cover classification only fires for **plugin** nodes whose nodedef
accepts ``FDUP`` / ``FDDOWN`` / ``FDSTOP`` *without* ``DON`` / ``DOF``
(otherwise pyisyox's classifier picks light or switch). The bundled
``eisy6_profile.json`` is a real anonymized capture of a stock eisy
which has no PG3 plugins, so cover-test fixtures inject a synthetic
plugin slot at runtime via ``pyisyox.testing.make_cover_load_result``.

Pin: ``Platform.COVER`` entity creation flowing through the real
``pyisyox.classify`` → ``ControllablePlatform.COVER`` →
``_CONTROLLABLE_TO_HA_PLATFORM`` path.
"""

from __future__ import annotations

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyisyox.testing import (
    make_controller,
    make_cover_load_result,
    make_plugin_cover_node_record,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    SnapshotAssertion,
    snapshot_platform,
)


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.COVER]


@pytest.fixture
def populated_controller():
    """Override the default fixture with a controller that carries the
    cover-plugin profile + a single cover node."""
    cover = make_plugin_cover_node_record()
    return make_controller(make_cover_load_result(nodes={cover.address: cover}))


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_cover_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every cover entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)
