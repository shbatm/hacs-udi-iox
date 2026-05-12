"""Snapshot tests for the udi_iox number platform."""

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
    return [Platform.NUMBER]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_number_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every number entity created by the integration."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_aux_on_level_uses_editor_units_both_directions() -> None:
    """``OL`` is the ``I_OL`` editor's UOM-51 0-100% quantity. A classic
    Insteon dimmer reports it as the raw UOM-100 0-255 byte on the wire,
    but pyisyox normalises that to the percentage on read — so the entity
    surfaces ``60`` for byte ``153`` — and writes the percentage straight
    through (``set_on_level(75)`` → pyisyox sends ``/cmd/OL/75/51``)."""
    from unittest.mock import AsyncMock, patch

    from pyisyox import NodePropertyValue
    from pyisyox.constants import PROP_ON_LEVEL

    from custom_components.udi_iox.models import IsyData
    from custom_components.udi_iox.number import CONTROL_DESC, ISYAuxControlNumberEntity
    from tests.builders import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    controller = make_controller(make_load_result())
    record = make_node_record(
        "AA AA AA 1",
        "Lamp",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="0", formatted="Off", uom="100", name="Status"
            ),
            # Raw wire form: a 0-255 byte. pyisyox normalises to 60% (UOM 51).
            "OL": NodePropertyValue(
                id="OL", value="153", formatted="60%", uom="100", name="On Level"
            ),
        },
    )
    node = make_node(record, controller)
    isy_data = IsyData()
    isy_data.root = controller
    entity = ISYAuxControlNumberEntity(
        isy_data,
        node=node,
        control=PROP_ON_LEVEL,
        unique_id="x_OL",
        description=CONTROL_DESC[PROP_ON_LEVEL],
        device_info=None,
    )

    assert entity.native_value == 60  # byte 153 → 60%, normalised by pyisyox

    set_on_level = AsyncMock()
    with patch.object(type(node), "set_on_level", set_on_level):
        await entity.async_set_native_value(75)

    assert set_on_level.await_args.args == (75,)
