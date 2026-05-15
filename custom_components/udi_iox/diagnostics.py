"""Diagnostics: redacted JSON snapshot of the entry + controller state.

Heavy lifting lives on :meth:`pyisyox.Controller.to_dict`; this module
adds entry/options redaction, MAC + portal-host masking, and
``IsyData`` shape counts. Redaction is narrow (PII only) — node
addresses and names stay verbatim so bug reports keep triage context.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .models import IsyConfigEntry

#: Keys redacted from ``entry.data``. Portal mode stores the user's
#: email at ``CONF_USERNAME``; ``CONF_HOST`` can carry the account
#: subdomain.
TO_REDACT_ENTRY_DATA = frozenset({CONF_PASSWORD, CONF_USERNAME, CONF_HOST})

#: Sentinel surfaced in place of redacted scalar values.
_REDACTED = "**REDACTED**"


def _redact_controller_uuid(uuid: str) -> str:
    """Mask the controller's MAC-shaped UUID.

    Keep the first three octets (the UD OUI ``00:21:b9``) so the device
    family stays identifiable in bug reports; blank the last three.
    Anything that doesn't parse as a 6-octet MAC redacts whole.
    """
    if not uuid:
        return ""
    parts = uuid.split(":")
    if len(parts) == 6:
        return ":".join([*parts[:3], "**", "**", "**"])
    return _REDACTED


def _redact_portal_host(portal_host: str | None) -> str | None:
    """Mask the portal hostname (``my-eisy-1234.isy.io``)."""
    if not portal_host:
        return None
    return _REDACTED


def _isy_data_shape(isy_data: Any) -> dict[str, Any]:
    """Per-platform entity counts from ``IsyData`` (HA-side only)."""

    def _platform_counts(mapping: dict[Any, list[Any]] | None) -> dict[str, int]:
        if not mapping:
            return {}
        return {str(platform): len(items) for platform, items in mapping.items()}

    return {
        "primary_nodes": _platform_counts(getattr(isy_data, "nodes", None)),
        "root_nodes": _platform_counts(getattr(isy_data, "root_nodes", None)),
        "aux_properties": _platform_counts(getattr(isy_data, "aux_properties", None)),
        "programs": _platform_counts(getattr(isy_data, "programs", None)),
        "variables": _platform_counts(getattr(isy_data, "variables", None)),
        "groups": len(getattr(isy_data, "groups", []) or []),
        "net_resources": len(getattr(isy_data, "net_resources", []) or []),
        "event_triggers": len(getattr(isy_data, "node_triggers", {}) or {}),
    }


def _redact_controller_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Apply PII masking to a ``Controller.to_dict()`` output in place.

    pyisyox returns raw values (CLI/file-dumper consumers want them);
    masking lives here because the diagnostics endpoint is the only
    redaction-sensitive consumer.
    """
    config = snapshot.get("config") or {}
    if "uuid" in config:
        config["uuid"] = _redact_controller_uuid(config["uuid"])
    if "portal_host" in config:
        config["portal_host"] = _redact_portal_host(config["portal_host"])
    return snapshot


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IsyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    isy_data = entry.runtime_data
    controller = isy_data.root

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "domain": entry.domain,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY_DATA),
            "options": dict(entry.options),
        },
        "controller": _redact_controller_snapshot(controller.to_dict()),
        "isy_data_shape": _isy_data_shape(isy_data),
        "event_triggers": {
            address: [cmd.id if hasattr(cmd, "id") else str(cmd) for cmd in commands]
            for address, commands in isy_data.node_triggers.items()
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: IsyConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one device — the matching IoX node(s)."""
    isy_data = entry.runtime_data
    controller = isy_data.root
    uuid = controller.config.uuid

    # KeypadLinc parents fold sub-buttons under one device, so the
    # diagnostic can legitimately span multiple addresses.
    matching_addresses: list[str] = []
    for _ident_domain, ident_id in device.identifiers:
        if ident_id.startswith(f"{uuid}_"):
            matching_addresses.append(ident_id[len(uuid) + 1 :])

    matched: list[dict[str, Any]] = []
    for address in matching_addresses:
        target = controller.nodes.get(address) or controller.groups.get(address)
        if target is not None:
            matched.append(target.to_dict())

    return {
        "device": {
            "id": device.id,
            "name": device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
        },
        "matched_addresses": matching_addresses,
        "nodes": matched,
    }
