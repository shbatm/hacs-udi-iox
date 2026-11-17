"""``ST`` aux sensors: created only where they show something new (#110).

``pyisyox.classify`` keeps ``ST`` as an aux control whenever no
controllable platform owns it. The consumer drops the sensor when the
node's status is already on another entity, and keeps it (with a
``{uid}_ST`` unique id that survives the stale-entity cleanup) when
``ST`` is the node's only reading.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.const import Platform, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyisyox.client import NodePropertyValue
from pyisyox.testing import make_controller, make_load_result, make_node_record
from pytest_homeassistant.common import MockConfigEntry

from custom_components.udi_iox.const import PLATFORMS
from custom_components.udi_iox.helpers import _categorize_nodes
from tests.conftest import isy_data_for

METER = "FF FF FF 1"


METER_SUB = "FF FF FF 2"


def _meter_record(address: str, name: str, **kwargs):
    return make_node_record(
        address,
        name,
        nodedef_id="IMETER_SOLO",
        type_="9.7.0.0",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="1234", formatted="1234 W", uom="73", name="Status"
            ),
        },
        **kwargs,
    )


def _meter_controller(*, with_sub_node: bool = False):
    """IMETER_SOLO nodes: ``ST`` (power) is each node's only reading.

    ``with_sub_node`` adds a second meter folded onto the first one's
    HA device, to check per-device dedup keeps both ``ST`` sensors.
    """
    records = [_meter_record(METER, "House Meter")]
    if with_sub_node:
        records.append(_meter_record(METER_SUB, "House Meter 2", pnode=METER))
    return make_controller(
        make_load_result(nodes={record.address: record for record in records})
    )


def _status_aux(controller) -> set[tuple[Platform, str]]:
    isy_data = isy_data_for(controller)
    _categorize_nodes(
        isy_data,
        controller.nodes,
        MappingProxyType({}),
        controller=controller,
        host="https://h",
    )
    return {
        (platform, node.address)
        for platform, items in isy_data.aux_properties.items()
        for node, control in items
        if control == "ST"
    }


def test_status_sensor_dropped_for_event_only_button(populated_controller) -> None:
    """A KeypadButton_ADV's ``ST`` rides on its event entity (#85), not a sensor."""
    assert (Platform.SENSOR, "AA AA AA 2") not in _status_aux(populated_controller)


def test_status_sensor_kept_when_it_is_the_only_reading() -> None:
    """An IMETER_SOLO root has no primary or event entity; ``ST`` stays (#54)."""
    assert (Platform.SENSOR, METER) in _status_aux(_meter_controller())


def test_status_sensor_not_deduped_across_sub_nodes() -> None:
    """Each node's only ``ST`` reading survives the per-device dedup."""
    assert {(Platform.SENSOR, METER), (Platform.SENSOR, METER_SUB)} <= _status_aux(
        _meter_controller(with_sub_node=True)
    )


async def test_setup_cleanup_removes_nothing(
    hass: HomeAssistant, populated_controller, mock_config_entry: MockConfigEntry
) -> None:
    """Every entity the platforms register is one ``unique_ids`` expects."""
    removed: list[str] = []
    original = er.EntityRegistry.async_remove

    def _spy(self: er.EntityRegistry, entity_id: str) -> None:
        removed.append(entity_id)
        original(self, entity_id)

    with patch.object(er.EntityRegistry, "async_remove", _spy):
        await _setup(hass, populated_controller, mock_config_entry, sorted(PLATFORMS))

    assert removed == []


async def test_status_sensor_survives_cleanup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The meter's ``ST`` sensor registers with ``{uid}_ST`` and stays."""
    await _setup(hass, _meter_controller(), mock_config_entry, [Platform.SENSOR])

    entries = [
        entry
        for entry in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        if entry.domain == Platform.SENSOR and entry.unique_id.endswith(f"{METER}_ST")
    ]
    assert len(entries) == 1
    state = hass.states.get(entries[0].entity_id)
    assert state is not None
    assert float(state.state) == 1234
    assert state.attributes["unit_of_measurement"] == UnitOfPower.WATT


async def _setup(
    hass: HomeAssistant,
    controller,
    entry: MockConfigEntry,
    platforms: list[Platform],
) -> None:
    """Run the real setup against ``controller`` with ``platforms`` forwarded."""
    entry.add_to_hass(hass)
    with (
        patch("custom_components.udi_iox.Controller", return_value=controller),
        patch("pyisyox.Controller.connect", new=AsyncMock(return_value=None)),
        patch("pyisyox.Controller.stop", new=AsyncMock(return_value=None)),
        patch("custom_components.udi_iox.PLATFORMS", platforms),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
