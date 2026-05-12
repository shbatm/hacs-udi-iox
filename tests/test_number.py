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


async def test_aux_on_level_scales_percent_to_byte_for_insteon() -> None:
    """A classic Insteon dimmer reports/accepts ``OL`` as a 0-255 byte;
    the HA slider is 0-100, so setting 100% must send byte 255 (not 100,
    which would land the device at ~39%)."""
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

    set_on_level = AsyncMock()
    with patch.object(type(node), "set_on_level", set_on_level):
        await entity.async_set_native_value(100)
        # ... and reading back the 0-255 byte shows the right percent.
        assert entity.native_value == 60  # 153 / 255 ≈ 60%

    assert set_on_level.await_args.args == (255,)
