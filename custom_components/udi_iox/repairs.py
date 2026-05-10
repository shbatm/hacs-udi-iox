"""Repair flows for the udi_iox integration.

Currently surfaces one issue:

* ``lifecycle_reload_required.{entry_id}`` — raised by
  :class:`.controller_events.IsyControllerEvents` when a reload-worthy
  ``NodeLifecycleEvent`` (added/removed/renamed/enabled/revised/
  removed-from-scene) arrives. The fix flow confirms with the user
  and then reloads the config entry so HA picks up the new node
  registry.
"""

from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


class LifecycleReloadRepairFlow(RepairsFlow):
    """Confirm → reload entry."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            await self.hass.config_entries.async_reload(self.entry.entry_id)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"title": self.entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Build the fix flow for a given issue."""
    if (
        issue_id.startswith("lifecycle_reload_required.")
        and data is not None
        and isinstance(data.get("entry_id"), str)
        and (entry := hass.config_entries.async_get_entry(data["entry_id"]))
    ):
        return LifecycleReloadRepairFlow(entry)

    raise ValueError(f"unknown repair {issue_id}")
