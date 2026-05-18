"""Diagnostics: redacted JSON snapshot of the entry + controller state.

Heavy lifting lives on :meth:`pyisyox.Controller.to_dict`; this module
adds entry/options redaction, MAC + portal-host masking, and
``IsyData`` shape counts. Redaction is narrow (PII only) — node
addresses and names stay verbatim so bug reports keep triage context.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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


def _redact_entry_title(title: str | None, host: str | None) -> str | None:
    """Strip the controller host out of a name.

    ``CONF_HOST`` is redacted in ``entry.data``, but the host still
    leaks: the config-entry title is ``"<name> (<hostname>)"`` and the
    hub device name falls back to the host when the controller has no
    name. Mask every spelling of the host (full value, ``netloc``, bare
    ``hostname``), longest first so a substring doesn't unmask the rest.
    """
    if not title or not host:
        return title
    parsed = urlparse(host)
    candidates = (c for c in (host, parsed.netloc, parsed.hostname) if c)
    for candidate in sorted(set(candidates), key=len, reverse=True):
        title = title.replace(candidate, _REDACTED)
    return title


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


def _mask_uuid(value: str | None, real_uuid: str, masked_uuid: str) -> str | None:
    """Swap the controller MAC for its masked form inside an id string."""
    if not value or not real_uuid:
        return value
    return value.replace(real_uuid, masked_uuid)


def _device_key(
    identifiers: set[tuple[str, str]], real_uuid: str, masked_uuid: str
) -> str | None:
    """Stable, readable handle for a device — its first (sorted)
    identifier with the MAC masked. Used instead of the HA registry's
    random per-run device UUID so the dump (and its snapshot) is
    deterministic and entity↔device links survive across runs.
    """
    for domain, ident in sorted(identifiers):
        return f"{domain}:{_mask_uuid(ident, real_uuid, masked_uuid)}"
    return None


def _registry_devices(
    hass: HomeAssistant,
    entry_id: str,
    host: str | None,
    key_by_id: dict[str, str | None],
) -> list[dict[str, Any]]:
    """HA device-registry rows for this entry — the devices actually
    created. Host-bearing names redacted; identity carried by the
    pre-masked ``key_by_id`` (``key`` / ``via_device``).
    """
    registry = dr.async_get(hass)
    return [
        {
            "key": key_by_id.get(d.id),
            "name": _redact_entry_title(d.name, host),
            "name_by_user": _redact_entry_title(d.name_by_user, host),
            "model": d.model,
            "manufacturer": d.manufacturer,
            "sw_version": d.sw_version,
            "via_device": key_by_id.get(d.via_device_id) if d.via_device_id else None,
            "area_id": d.area_id,
            "entry_type": d.entry_type,
        }
        for d in dr.async_entries_for_config_entry(registry, entry_id)
    ]


def _registry_entities(
    hass: HomeAssistant,
    entry_id: str,
    real_uuid: str,
    masked_uuid: str,
    key_by_id: dict[str, str | None],
) -> list[dict[str, Any]]:
    """HA entity-registry rows for this entry — the entities actually
    created, by ``entity_id``. ``device`` is the owning device's stable
    key (correlates with ``devices[].key``). The controller MAC is the
    only PII (it prefixes every ``unique_id``); node names/addresses
    stay verbatim for triage, matching this module's narrow-redaction
    policy.
    """
    registry = er.async_get(hass)
    return [
        {
            "entity_id": e.entity_id,
            "unique_id": _mask_uuid(e.unique_id, real_uuid, masked_uuid),
            "platform": e.platform,
            "device": key_by_id.get(e.device_id) if e.device_id else None,
            "disabled_by": e.disabled_by,
            "hidden_by": e.hidden_by,
            "entity_category": e.entity_category,
            "original_name": e.original_name,
            "translation_key": e.translation_key,
        }
        for e in er.async_entries_for_config_entry(registry, entry_id)
    ]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IsyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    isy_data = entry.runtime_data
    controller = isy_data.root
    host = entry.data.get(CONF_HOST)
    real_uuid = controller.config.uuid
    masked_uuid = _redact_controller_uuid(real_uuid)

    # Map each device's random HA-registry id → its stable key so the
    # dump never carries a non-deterministic UUID (snapshot-safe) yet
    # entity→device / device→via_device links stay intact.
    key_by_id = {
        d.id: _device_key(d.identifiers, real_uuid, masked_uuid)
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    }

    return {
        "entry": {
            "title": _redact_entry_title(entry.title, host),
            "version": entry.version,
            "domain": entry.domain,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY_DATA),
            "options": dict(entry.options),
        },
        "controller": _redact_controller_snapshot(controller.to_dict()),
        "devices": _registry_devices(hass, entry.entry_id, host, key_by_id),
        "entities": _registry_entities(
            hass, entry.entry_id, real_uuid, masked_uuid, key_by_id
        ),
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
            "name": _redact_entry_title(device.name, entry.data.get(CONF_HOST)),
            "model": device.model,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
        },
        "matched_addresses": matching_addresses,
        "nodes": matched,
    }
