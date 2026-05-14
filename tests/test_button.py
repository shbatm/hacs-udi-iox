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
    """``WDU`` ("Write Changes") plus the Insteon "fast on/off" and
    momentary paddle verbs (``DFON``/``DFOF``/``BRT``/``DIM``/``FDUP``/
    ``FDDOWN``/``FDSTOP``) are created disabled by default, while
    everyday verbs are enabled."""
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeCommandButtonEntity
    from custom_components.udi_iox.models import IsyData

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

    for verb in ("WDU", "DFON", "DFOF", "BRT", "DIM", "FDUP", "FDDOWN", "FDSTOP"):
        assert _button(verb).entity_registry_enabled_default is False, verb
    assert _button("DISCOVER").entity_registry_enabled_default is True
    assert _button("BEEP").entity_registry_enabled_default is True


async def test_command_button_translates_node_command_error() -> None:
    """A controller-side rejection on a plugin-command button press
    becomes HomeAssistantError so HA surfaces the failure to the user
    (was previously unhandled — the raw NodeCommandError bubbled out
    of async_press)."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeCommandButtonEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = IsyData()
    button = ISYNodeCommandButtonEntity(
        isy_data,
        node,
        command_id="DISCOVER",
        name="Discover",
        unique_id="x_discover",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to send DISCOVER"),
    ):
        await button.async_press()


async def test_beep_button_translates_node_command_error() -> None:
    """BEEP button surfaces NodeCommandError as HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeBeepButtonEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = IsyData()
    button = ISYNodeBeepButtonEntity(
        isy_data,
        node,
        name="Beep",
        unique_id="x_beep",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to beep node"),
    ):
        await button.async_press()


async def test_query_button_translates_node_command_error() -> None:
    """Node-target Query button surfaces NodeCommandError as HomeAssistantError
    using the node address (not the button's HA name)."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import Node, NodeCommandError
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_node,
        make_node_record,
    )

    from custom_components.udi_iox.button import ISYNodeQueryButtonEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    node = make_node(make_node_record("AA AA AA 1", "Lamp"), controller)
    isy_data = IsyData()
    button = ISYNodeQueryButtonEntity(
        isy_data,
        node=node,
        name="Query",
        unique_id="x_query",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Node, "send_command", new=AsyncMock(side_effect=NodeCommandError("nope"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to query node AA AA AA 1"),
    ):
        await button.async_press()


async def test_query_button_translates_controller_refresh_failure() -> None:
    """Controller-target Query button surfaces a broad-except as
    HomeAssistantError using the controller's UUID."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant import exceptions as ha_exceptions
    from pyisyox import Controller
    from pyisyox.testing import make_controller, make_load_result

    from custom_components.udi_iox.button import ISYNodeQueryButtonEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    isy_data = IsyData()
    button = ISYNodeQueryButtonEntity(
        isy_data,
        node=controller,
        name="Query",
        unique_id="x_query",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            Controller, "refresh", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(
            ha_exceptions.HomeAssistantError, match="Unable to refresh controller"
        ),
    ):
        await button.async_press()


async def test_network_resource_button_translates_run_failure() -> None:
    """Network-resource button surfaces a broad-except as HomeAssistantError."""
    from unittest.mock import AsyncMock, patch

    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from pyisyox import NetworkResource
    from pyisyox.testing import (
        make_controller,
        make_load_result,
        make_network_resource_record,
    )

    from custom_components.udi_iox.button import ISYNetworkResourceButtonEntity
    from custom_components.udi_iox.models import IsyData

    controller = make_controller(make_load_result())
    record = make_network_resource_record("1", "Doorbell")
    resource = NetworkResource(record, controller._client)
    isy_data = IsyData()
    button = ISYNetworkResourceButtonEntity(
        isy_data,
        node=resource,
        name=resource.name,
        unique_id="x_netres",
        device_info=None,  # type: ignore[arg-type]
    )
    with (
        patch.object(
            NetworkResource, "run", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="Unable to run network resource"),
    ):
        await button.async_press()
