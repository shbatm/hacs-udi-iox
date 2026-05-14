"""Device triggers for udi_iox event entities.

HA's built-in state-trigger requires a value transition: pressing the
same KeypadLinc button twice doesn't fire a trigger configured with
``attribute: event_type`` + ``to: "off"`` because the attribute didn't
change. The :class:`event.EventEntity` updates its state timestamp on
every press, but ``state_trigger`` without an ``attribute`` filter then
fires for *every* event_type.

This module exposes one device trigger per (event entity, event_type),
so users get a UI-driven picker that fires on every dispatched event of
the chosen type — matching what users coming from legacy
``isy994_control`` automations expect.

The trigger watches the entity's state via ``async_track_state_change_event``
and matches against the entity's ``event_type`` attribute on each change,
so consecutive same-type events all fire.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.components.device_automation import (
    DEVICE_TRIGGER_BASE_SCHEMA,
    InvalidDeviceAutomationConfig,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TYPE,
    Platform,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .event import EVENT_BUTTON_UNIQUE_ID_SUFFIX, event_type_for_command

if TYPE_CHECKING:
    from .models import IsyData

CONF_SUBTYPE = "subtype"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): str,
        vol.Required(CONF_ENTITY_ID): str,
        vol.Optional(CONF_SUBTYPE): str,
    }
)


def _resolve_isy_data(hass: HomeAssistant, device_id: str) -> IsyData | None:
    """Return the loaded :class:`IsyData` backing ``device_id``, if any."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            return cast("IsyData", entry.runtime_data)
    return None


def _event_entries_for_device(
    hass: HomeAssistant, device_id: str, isy_data: IsyData
) -> Iterator[tuple[er.RegistryEntry, str, list[str]]]:
    """Yield ``(entry, node_address, event_types)`` per event entity on the device."""
    suffix = EVENT_BUTTON_UNIQUE_ID_SUFFIX
    prefix = f"{isy_data.uuid}_"
    for entry in er.async_entries_for_device(
        er.async_get(hass), device_id, include_disabled_entities=True
    ):
        if entry.platform != DOMAIN or entry.domain != Platform.EVENT.value:
            continue
        unique_id = entry.unique_id
        if not (unique_id.startswith(prefix) and unique_id.endswith(suffix)):
            continue
        address = unique_id[len(prefix) : -len(suffix)]
        commands = isy_data.node_triggers.get(address)
        if not commands:
            continue
        types = list(dict.fromkeys(event_type_for_command(cmd) for cmd in commands))
        if types:
            yield entry, address, types


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List one trigger per (event entity, event_type) on ``device_id``."""
    isy_data = _resolve_isy_data(hass, device_id)
    if isy_data is None:
        return []
    triggers: list[dict[str, str]] = []
    for entry, address, types in _event_entries_for_device(hass, device_id, isy_data):
        triggers.extend(
            {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                CONF_ENTITY_ID: entry.entity_id,
                CONF_TYPE: event_type,
                CONF_SUBTYPE: address,
            }
            for event_type in types
        )
    return triggers


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, Any]:
    """No additional fields — type + entity_id fully describe the trigger."""
    return {}


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Fire ``action`` on every state change whose ``event_type`` attribute matches."""
    entity_id: str = config[CONF_ENTITY_ID]
    target_type: str = config[CONF_TYPE]
    subtype: str = config.get(CONF_SUBTYPE, "")
    device_id: str = config[CONF_DEVICE_ID]
    if er.async_get(hass).async_get(entity_id) is None:
        raise InvalidDeviceAutomationConfig(
            f"udi_iox event entity {entity_id} not found"
        )
    job = HassJob(
        action,
        f"udi_iox device trigger {entity_id} {target_type}",
    )
    trigger_data = trigger_info["trigger_data"]
    description = (
        f"event '{target_type}' on {entity_id}"
        if not subtype
        else f"event '{target_type}' on {entity_id} ({subtype})"
    )

    @callback
    def _state_changed(event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is None:
            return
        # Skip transitions out of "unavailable" — those happen on WS
        # reconnect when the EventEntity restores its prior timestamp +
        # event_type; not a real press. "unknown" is allowed because
        # that's the entity's pre-first-press state on a fresh reload,
        # and the first real press transitions out of it.
        old_state = event.data["old_state"]
        if old_state is not None and old_state.state == "unavailable":
            return
        if new_state.attributes.get("event_type") != target_type:
            return
        hass.async_run_hass_job(
            job,
            {
                "trigger": {
                    **trigger_data,
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    "entity_id": entity_id,
                    "type": target_type,
                    "subtype": subtype,
                    "description": description,
                }
            },
            event.context,
        )

    return async_track_state_change_event(hass, [entity_id], _state_changed)
