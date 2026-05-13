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


async def test_variable_number_scales_by_precision_on_read_and_write() -> None:
    """IoX variables store a raw integer on the wire; ``precision``
    declares the implicit decimal shift. The HA number entity speaks
    *displayed* units (its step / min / max all assume that), so:

    * ``native_value`` divides raw by ``10**precision`` — wire ``50``
      with prec=1 surfaces as 5.0, not 50.
    * ``async_set_native_value`` multiplies and rounds — entering 70.5
      with prec=1 lands raw 705 (not 70, which is what ``int(70.5)``
      would have given pre-fix).
    """
    from unittest.mock import AsyncMock, patch

    from pyisyox.client import VariableRecord
    from pyisyox.runtime import Variable

    from custom_components.udi_iox.models import IsyData
    from custom_components.udi_iox.number import ISYVariableNumberEntity
    from tests.builders import make_controller, make_load_result

    controller = make_controller(make_load_result())
    record = VariableRecord(
        type_id="2",
        id="5",
        name="Temp",
        value=50,
        init=70,
        precision=1,
    )
    variable = Variable(record, controller._client)
    isy_data = IsyData()
    isy_data.root = controller

    from homeassistant.components.number import NumberEntityDescription

    desc = NumberEntityDescription(
        key="2.5",
        name="Temp",
        native_step=0.1,
        native_min_value=-1000,
        native_max_value=1000,
    )
    value_entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=desc,
        device_info=None,  # type: ignore[arg-type]
    )
    init_entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="vi",
        description=desc,
        device_info=None,  # type: ignore[arg-type]
        init_entity=True,
    )

    # Read side: raw 50 / 10**1 → 5.0; raw 70 → 7.0.
    assert value_entity.native_value == 5.0
    assert init_entity.native_value == 7.0

    # Write side: HA sends displayed 70.5 → wire 705 (not int(70.5)=70).
    # ``async_write_ha_state`` requires a live ``hass`` binding; patch it
    # out so the entity body runs in isolation.
    set_value = AsyncMock()
    set_init = AsyncMock()
    with (
        patch.object(type(variable), "set_value", set_value),
        patch.object(type(variable), "set_init", set_init),
        patch.object(
            ISYVariableNumberEntity, "async_write_ha_state", lambda self: None
        ),
    ):
        await value_entity.async_set_native_value(70.5)
        await init_entity.async_set_native_value(0.5)

    assert set_value.await_args.args == (705,)
    assert set_init.await_args.args == (5,)


async def test_variable_number_pass_through_when_precision_zero() -> None:
    """A variable with ``precision=0`` round-trips raw ints — no
    scaling. Catches the obvious regression where the precision math
    runs unconditionally and shifts an integer-only variable."""
    from unittest.mock import AsyncMock, patch

    from pyisyox.client import VariableRecord
    from pyisyox.runtime import Variable

    from custom_components.udi_iox.models import IsyData
    from custom_components.udi_iox.number import ISYVariableNumberEntity
    from tests.builders import make_controller, make_load_result

    controller = make_controller(make_load_result())
    record = VariableRecord(
        type_id="1", id="3", name="Count", value=42, init=0, precision=0
    )
    variable = Variable(record, controller._client)
    isy_data = IsyData()
    isy_data.root = controller

    from homeassistant.components.number import NumberEntityDescription

    desc = NumberEntityDescription(
        key="1.3", name="Count", native_step=1, native_min_value=0, native_max_value=255
    )
    entity = ISYVariableNumberEntity(
        isy_data,
        variable,
        unique_id="v",
        description=desc,
        device_info=None,  # type: ignore[arg-type]
    )

    assert entity.native_value == 42  # no scaling

    set_value = AsyncMock()
    with (
        patch.object(type(variable), "set_value", set_value),
        patch.object(
            ISYVariableNumberEntity, "async_write_ha_state", lambda self: None
        ),
    ):
        await entity.async_set_native_value(99)

    assert set_value.await_args.args == (99,)
