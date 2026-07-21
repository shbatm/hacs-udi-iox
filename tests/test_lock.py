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

from tests.conftest import isy_data_for


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


# --- Direct entity tests (cover up the non-snapshot logic) ---


async def test_lock_attrs_unknown_status_yields_none() -> None:
    """Status absent → is_locked is None."""
    from unittest.mock import patch

    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.lock import ISYLockEntity

    controller = make_controller(make_load_result())
    record = make_node_record(
        "L 1",
        "Door",
        properties={
            "ST": NodePropertyValue(
                id="ST", value="", formatted="", uom="11", name="Status"
            )
        },
    )
    node = make_node(record, controller)
    isy_data = isy_data_for(controller)
    entity = ISYLockEntity(isy_data, node=node, device_info=None)
    # async_write_ha_state is synchronous in real HA -- a plain Mock,
    # not AsyncMock (which would return an unawaited coroutine here).
    with patch.object(entity, "async_write_ha_state"):
        entity.async_on_update(None, "")  # type: ignore[arg-type]
    assert entity.is_locked is None


async def test_lock_attrs_track_value_to_state_mapping() -> None:
    """ST=100 → locked True; ST=0 → locked False; other → None."""
    from unittest.mock import patch

    from pyisyox.client import NodePropertyValue
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.lock import ISYLockEntity

    controller = make_controller(make_load_result())
    isy_data = isy_data_for(controller)

    for raw, expected in [("100", True), ("0", False), ("50", None)]:
        record = make_node_record(
            "L 1",
            "Door",
            properties={
                "ST": NodePropertyValue(
                    id="ST", value=raw, formatted=raw, uom="11", name="Status"
                )
            },
        )
        node = make_node(record, controller)
        entity = ISYLockEntity(isy_data, node=node, device_info=None)
        with patch.object(entity, "async_write_ha_state"):
            entity.async_on_update(None, "")  # type: ignore[arg-type]
        assert entity.is_locked is expected, f"raw={raw}"


async def test_lock_secure_translates_node_command_errors() -> None:
    """Both lock and unlock raise HomeAssistantError on rejection."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.lock import ISYLockEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("L 1", "Door"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYLockEntity(isy_data, node=node, device_info=None)
    with (
        patch.object(
            Node, "secure_lock", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to lock device"),
    ):
        await entity.async_lock()
    with (
        patch.object(
            Node, "secure_unlock", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to unlock device"),
    ):
        await entity.async_unlock()


async def test_lock_zwave_user_code_services_reject() -> None:
    """Both Z-Wave user-code services raise HomeAssistantError — these
    paths exist for service-API compatibility but aren't implemented."""
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.lock import ISYLockEntity

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("L 1", "Door"), controller)
    isy_data = isy_data_for(controller)
    entity = ISYLockEntity(isy_data, node=node, device_info=None)
    with pytest.raises(HomeAssistantError, match="not supported"):
        await entity.async_set_zwave_lock_user_code(1, 1234)
    with pytest.raises(HomeAssistantError, match="not supported"):
        await entity.async_delete_zwave_lock_user_code(1)


async def test_lock_program_entity() -> None:
    """Program lock: is_locked reads status program; lock/unlock delegate."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Program
    from pyisyox.testing import make_controller, make_load_result, make_program_record

    from custom_components.udi_iox.lock import ISYLockProgramEntity

    controller = make_controller(make_load_result())
    status = Program(
        make_program_record("0001", "Status", status=True), controller._client
    )
    actions = Program(make_program_record("0002", "Actions"), controller._client)
    isy_data = isy_data_for(controller)
    entity = ISYLockProgramEntity(isy_data, "FrontDoor", status, actions)

    assert entity.is_locked is True

    with (
        patch.object(
            Program, "run_then", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to lock program"),
    ):
        await entity.async_lock()
    with (
        patch.object(
            Program, "run_else", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to unlock program"),
    ):
        await entity.async_unlock()

    with patch.object(Program, "run_then", new=AsyncMock()) as run_then:
        await entity.async_lock()
    run_then.assert_awaited_once()
    with patch.object(Program, "run_else", new=AsyncMock()) as run_else:
        await entity.async_unlock()
    run_else.assert_awaited_once()
