"""Tests for the udi_iox device-trigger platform."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.components.device_automation import (
    DeviceAutomationType,
    InvalidDeviceAutomationConfig,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.udi_iox.const import DOMAIN
from custom_components.udi_iox.device_trigger import CONF_SUBTYPE


@pytest.fixture
def platforms() -> list:
    """Forward only the event platform for these tests."""
    from homeassistant.const import Platform

    return [Platform.EVENT]


def _device_for_event_entity(
    hass: HomeAssistant, entity_id: str
) -> dr.DeviceEntry | None:
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None, f"event entity {entity_id} not registered"
    assert entry.device_id is not None, f"event entity {entity_id} has no device"
    return dr.async_get(hass).async_get(entry.device_id)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_get_triggers_lists_per_event_type(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """async_get_triggers returns one entry per (entity, event_type)."""
    # The hallway sub-button (KeypadLinc-style) is the only fixture node
    # routed to Platform.EVENT — it folds under its parent device, so its
    # device is the hallway-light root.
    entity_id = "event.hallway_light_hallway_button_b"
    device = _device_for_event_entity(hass, entity_id)
    assert device is not None

    triggers = [
        t
        for t in await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, device.id
        )
        if t.get(CONF_DOMAIN) == DOMAIN
    ]

    # Each event entity exposes one trigger per slugified sent-command name.
    assert triggers, "expected at least one device trigger"
    types = {t[CONF_TYPE] for t in triggers}
    assert {"on", "off"}.issubset(types)
    assert all(t[CONF_DEVICE_ID] == device.id for t in triggers)
    assert all(t[CONF_SUBTYPE] for t in triggers)


async def test_get_triggers_unknown_device_returns_empty(
    hass: HomeAssistant,
) -> None:
    """A device id our integration doesn't back yields no triggers."""
    from custom_components.udi_iox.device_trigger import async_get_triggers

    assert await async_get_triggers(hass, "does-not-exist") == []


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_attach_trigger_fires_on_matching_event_type(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    service_calls: list[ServiceCall],
) -> None:
    """The action runs each time the entity's event_type attribute matches."""
    entity_id = "event.hallway_light_hallway_button_b"
    device = _device_for_event_entity(hass, entity_id)
    assert device is not None

    assert await async_setup_component(
        hass,
        AUTOMATION_DOMAIN,
        {
            AUTOMATION_DOMAIN: [
                {
                    "trigger": [
                        {
                            CONF_PLATFORM: "device",
                            CONF_DOMAIN: DOMAIN,
                            CONF_DEVICE_ID: device.id,
                            "entity_id": entity_id,
                            CONF_TYPE: "on",
                        }
                    ],
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )
    await hass.async_block_till_done()

    # Two consecutive 'on' presses — both must fire (real EventEntity
    # advances the timestamp on each press; this is the specific
    # behaviour HA's state-trigger drops on identical event_type).
    for ts in ("2026-05-14T18:00:00+00:00", "2026-05-14T18:00:01+00:00"):
        hass.states.async_set(
            entity_id,
            ts,
            {"event_type": "on", "event_types": ["on", "off"]},
        )
        await hass.async_block_till_done()

    # Then a non-matching press — must not fire.
    hass.states.async_set(
        entity_id,
        "2026-05-14T18:00:02+00:00",
        {"event_type": "off", "event_types": ["on", "off"]},
    )
    await hass.async_block_till_done()

    fired = [c for c in service_calls if c.domain == "test"]
    assert len(fired) == 2


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_attach_trigger_does_not_fire_on_reconnect(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    service_calls: list[ServiceCall],
) -> None:
    """A WS reconnect (unavailable → known) must not re-fire the last press."""
    from homeassistant.const import STATE_UNAVAILABLE

    entity_id = "event.hallway_light_hallway_button_b"
    device = _device_for_event_entity(hass, entity_id)
    assert device is not None

    assert await async_setup_component(
        hass,
        AUTOMATION_DOMAIN,
        {
            AUTOMATION_DOMAIN: [
                {
                    "trigger": [
                        {
                            CONF_PLATFORM: "device",
                            CONF_DOMAIN: DOMAIN,
                            CONF_DEVICE_ID: device.id,
                            "entity_id": entity_id,
                            CONF_TYPE: "on",
                        }
                    ],
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )
    await hass.async_block_till_done()

    # Real press — should fire.
    hass.states.async_set(
        entity_id,
        "2026-05-14T18:00:00+00:00",
        {"event_type": "on", "event_types": ["on", "off"]},
        force_update=True,
    )
    await hass.async_block_till_done()

    # WS drops → entity goes unavailable.
    hass.states.async_set(entity_id, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()

    # WS reconnects → entity restores its prior state + event_type — NOT a press.
    hass.states.async_set(
        entity_id,
        "2026-05-14T18:00:00+00:00",
        {"event_type": "on", "event_types": ["on", "off"]},
        force_update=True,
    )
    await hass.async_block_till_done()

    fired = [c for c in service_calls if c.domain == "test"]
    assert len(fired) == 1, "reconnect must not re-fire the last press"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_attach_trigger_unknown_entity_raises(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Configuring a trigger against an unknown entity raises."""
    from custom_components.udi_iox.device_trigger import async_attach_trigger

    with pytest.raises(InvalidDeviceAutomationConfig):
        await async_attach_trigger(
            hass,
            {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: "ignored",
                "entity_id": "event.does_not_exist",
                CONF_TYPE: "on",
            },
            AsyncMock(),
            {"trigger_data": {}, "name": "test", "home_assistant_start": False},
        )


async def test_get_capabilities_is_empty(hass: HomeAssistant) -> None:
    """No extra capability fields beyond type/entity_id."""
    from custom_components.udi_iox.device_trigger import (
        async_get_trigger_capabilities,
    )

    assert await async_get_trigger_capabilities(hass, {}) == {}
