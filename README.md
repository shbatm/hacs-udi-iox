# hacs-udi-iox

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Home Assistant custom component for **Universal Devices eisy** controllers running **IoX 6.0+**.

## What's new vs. the core `isy994` integration

If you've used HA's built-in `isy994` for years, the things this integration adds on top:

- **IoX programs are first-class HA devices.** Each program (and program folder) becomes its own device with `Run` / `Run Then` / `Run Else` / `Stop` / `Enable` / `Disable` buttons, an enable switch, a *Run at startup* toggle, and `Last Run` / `Last Finished` / `Next Scheduled Run` timestamp sensors. Triggering and observing IoX programs no longer requires a status program + actions program pair plus YAML.
- **Native event entities for keypad / scene buttons.** KeypadLinc accessory buttons, RemoteLincs, and any nodedef with a `cmds.sends` list become `event.*` entities exposed through HA's device-trigger UI — no `udi_iox_control` bus events, no listening for raw control codes. Fires on every press, including same-type repeats. Replaces the legacy `sensor.<keypad>_<letter>` integer-state hack.
- **Scene members in the more-info dialog.** Each IoX scene (group) publishes its member nodes through HA's group framework, so opening a scene lists its member lights / switches / fans inline (mixed domains, same UX as a HA light group). Core `isy994` exposes scenes as bare switches with no member visibility. (Hidden member entities are omitted by HA's frontend, as with any group.)
- **Suggested area from your IoX folder layout.** Organize nodes or programs into folders on the controller (`Kitchen`, `Garage`, …) and HA pre-fills each device's *Suggested Area* from the immediate parent folder.
- **Dynamic device support.** Core `isy994` carries several hardcoded type-prefix tables to decide which HA platform each Insteon device belongs on — adding a new device class meant editing the integration. This integration reads each device's **nodedef from the controller** (the wire shape: accepts / sends / properties / editors). Insteon devices land on the same baseline platforms as core, but Z-Wave, Zigbee, Matter, and any PG3 node-server plugin all classify themselves automatically with no integration update needed.
- **Z-Wave / Insteon configurables as native HA entities.** On Level, Ramp Rate, Backlight, ramp/scene values, Z-Wave configuration parameters — anything the controller publishes as an editor surfaces as a `number` / `select` / `switch` entity (no service calls, no YAML).
- **WebSocket-only event stream + Repair-card lifecycle UX.** State updates ride a single WebSocket; entity availability follows WS health. New / removed / renamed devices on the controller surface as Repair cards prompting a reload instead of being silently missed.

> **Side-by-side notes:** the two integrations register distinct domains (`isy994` vs. `udi_iox`) and coexist on the same HA instance — useful for ISY-994 + eisy mixed setups. See [`docs/coexistence-with-core.md`](docs/coexistence-with-core.md) for a per-platform comparison.

## Legacy Hardware Scope

If you have ISY-994 hardware, use the existing Home Assistant core [`isy994` integration](https://www.home-assistant.io/integrations/isy994/) (which stays on `pyisy` 3.x). The two integrations register distinct domains and coexist on the same HA instance.

The earlier-generation **Polisy** controller is end-of-life and no longer a target for this integration — the SSDP / DHCP discovery matchers have been narrowed to eisy. Polisy users can still configure the integration manually (the IoX 6 protocol is the same), but auto-discovery won't surface a Polisy on the network.

## Why a separate repo

`hacs-isy994` was a stable beta-testing channel for the upstream `isy994` integration which served its purpose, but I no longer want to maintain legacy hardware support for Home Assistant's core `isy994` and try to maintain both new features and legacy support in another repo. The eisy / IoX-6+ rewrite is a clean break: different library (`pyisyox` v6 with JWT/portal auth, WebSocket-only, classifier-driven entity routing, ergonomic Node wrappers). Forcing existing `hacs-isy994` users onto that rewrite would regress their working ISY-994 setups, so this is a new repo with a new domain.

## Installation

> **IF YOU ARE USING THE CORE ISY994 INTEGRATION:** While they can co-exist side-by-side, you may wish to remove that integration before adding this one. For the large portion of entities that overlap, they will try to get the same `entity_id` and you will end up with everything suffixed with `domain.*_2`. To keep your dashboards intact as much as possible, remove the `isy994` integration, restart HA once (needed for installing this repo anyways) and then add this integration to restore the entities. Any `entity_id` that you manually renamed on the old integration will need to be renamed again.

### Prerequisites

Before starting the setup flow, gather:

- A **Universal Devices portal account** (the same email + password you use to sign in at <https://my.isy.io>). The eisy on IoX 6+ authenticates against the portal — local admin credentials are not supported by the integration yet.
  - If you don't have a portal account, follow the "Sign up" link on <https://my.isy.io> and link your controller there first.
- The **base URL of your eisy**, including the scheme and port. Examples:
  - `https://eisy.local:443` (default portal-mode port on the local network)
  - `https://192.168.1.50:443` (by IP)
  - `https://eisy.example.com:443` (with a non-default hostname)

  The default port for portal (JWT) auth is `443`. Visit `https://<your-controller>/desc` in a browser to confirm the controller responds — if you get a self-signed-cert warning, that's normal.

### HACS install

This is a HACS Custom Repository:

1. Open HACS in Home Assistant: [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?repository=hacs-udi-iox&owner=shbatm&category=integration) (or HACS → ⋮ → Custom repositories → add `https://github.com/shbatm/hacs-udi-iox`, category Integration).
2. Install, restart HA.
3. Add the integration: [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=udi_iox) (or Settings → Devices & Services → **+ Add Integration** → search "Universal Devices IoX").

### Setup parameters

The initial setup flow asks for the following. HA also recognises eisy controllers via SSDP and DHCP discovery — if your controller is on the same network as HA, the integration usually surfaces it automatically and prefills the `host` field; you only need to enter credentials.

| Parameter | Required | Description |
| --- | --- | --- |
| **URL** (`host`) | yes | The full controller URL, e.g. `https://eisy.local:443`. Use the same URL you'd open in a browser. |
| **Universal Devices portal email** (`username`) | yes | The email you registered at <https://my.isy.io>. |
| **Universal Devices portal password** (`password`) | yes | The password for that portal account. |
| **Verify SSL certificate** (`verify_ssl`) | no (default: off) | Leave **off** unless you've installed a CA-trusted certificate on the controller — eisys ship with a self-signed cert that will fail verification. |

After the credentials step the flow lands on the **Options** step (the same form is later available via the integration card's **Configure** button — see [Configuration](#configuration) below).

## Removal

1. Settings → Devices & Services → Universal Devices IoX → ⋮ → Delete (removes the config entry, its devices, and its entities).
2. Optionally remove the repository in HACS (Integrations → Universal Devices IoX → ⋮ → Remove) and restart HA.

## Configuration

All of these are revisitable at any time via **Settings → Devices & Services → Universal Devices IoX → Configure**. Changing them triggers a reload, so the new values take effect immediately.

| Option | Default | Description |
| --- | --- | --- |
| **Node Sensor String** (`sensor_string`) | `{SENSOR}` | A node whose IoX name contains this exact string is forced onto the (binary) sensor platform instead of its classifier-assigned one (e.g. a generic Insteon I/O Linc input). The marker is matched verbatim **and stripped from the HA device/entity name** (so `Garbage Disposal {SENSOR}` → `Garbage Disposal`). Use a distinctive bracketed marker — the bracketed default avoids matching node-server entities that legitimately contain the word "sensor". |
| **Ignore String** (`ignore_string`) | `{IGNORE ME}` | Any device, **program, or folder** whose IoX name contains this substring is skipped entirely — no entity/device is created. Add the substring to a node's, program's, or folder's name in the eisy admin UI to hide it (and everything under a folder) from HA without deleting it from the controller. |
| **Restore Light Brightness** (`restore_light_state`) | off | When **on**, turning a light on from HA restores the previous brightness instead of using the device's built-in On Level (Insteon `OL`). When **off**, the device decides the start level — usually the right call for Insteon switches with a configured On Level. |
| **Enable adding variables entities** (`enable_variables`) | on | When **off**, IoX variables (Integer + State) are not exposed as `number` entities. Turn off if you have many variables and only use them inside IoX programs (faster startup). |
| **Enable adding program entities** (`enable_programs`) | on | When **off**, IoX programs are not exposed as switches / covers / locks / etc. Turn off if you don't drive IoX programs from HA. |
| **Enable adding network resource entities** (`enable_networking`) | off | When **on**, IoX network resources (HTTP / TCP / UDP triggers configured under Network → Network Resources in the admin UI) are surfaced as buttons. |

### Reauthentication

If you change your Universal Devices portal password the integration will detect the failed login on the next refresh and surface a Repair card prompting you to reauthenticate. The reauth flow asks for the username + password only — the host stays as-is.

## Automations on button presses

Insteon KeypadLinc accessory buttons (and any nodedef with a `cmds.sends`
list — most Insteon load/dimmer controls and many PG3 plugin sources)
are exposed as `event` entities. Each press updates the entity's state
timestamp and sets its `event_type` attribute (`on`, `off`, `fast_on`,
`fast_off`, `fade_up`, `fade_down`, `fade_stop`, …).

**Use a device trigger for "fire on every press."** Settings →
Automations & Scenes → Create Automation → When → Add trigger → Device,
pick the KeypadLinc / SwitchLinc / etc., and choose e.g. *"… was switched
On"*. This fires every time the button is pressed, even when the previous
press was the same type — which is the behaviour most Insteon users
expect.

Equivalent YAML:

```yaml
trigger:
  - platform: device
    domain: udi_iox
    device_id: <copy from the UI>
    entity_id: event.hallway_keypad_b
    type: "on"
action:
  - service: light.toggle
    target:
      entity_id: light.kitchen_main
```

**Why not a state trigger?** A state trigger configured with `attribute:
event_type` and `to: "on"` only fires when the attribute *changes*: pressing
the same button twice in a row is the same `event_type` value both times,
so the second press is silently dropped. The device trigger above sidesteps
this by matching against the new state's `event_type` on every update,
not just transitions.

## Supported devices

The integration surfaces every device on your controller — Insteon, Z-Wave, Zigbee, Matter, and any [PG3 node-server plugin](https://www.universal-devices.com/polyglot/) you have installed. HA platform routing is driven by `pyisyox`'s classifier reading each device's nodedef, not a hard-coded table.

If you bridge a device through a PG3 plugin **and** Home Assistant has a first-party integration for that same device (Sonos, Hue, Rachio, Roku, Plex, Ecobee, Shelly, WLED, etc.), install HA's native integration directly. It will almost always be the better source of truth — local-push state, manufacturer-aware quirks, more device-specific features. The two coexist fine: this integration still exposes the device's IoX-side state (useful if your IoX programs reference the device), and the native integration handles the device itself.

See [`docs/supported-devices.md`](docs/supported-devices.md) for the full list of PG3 plugins with HA-native equivalents, the cases where the IoX path is the right call, and roadmap notes for the platforms still to come.

## Reporting issues

Bug reports go through [GitHub Issues](https://github.com/shbatm/hacs-udi-iox/issues/new/choose) — the **Bug report** template walks you through the diagnostics + debug-log attachments most issues need. Two things to grab before you file:

### 1. Diagnostics file

Captures a redacted snapshot of what the integration sees: nodes, programs, variables, WebSocket health, and the entry's options. It's the fastest way to confirm the wire shape of whatever's misbehaving. ([HA docs on diagnostics](https://www.home-assistant.io/integrations/diagnostics/))

1. Open the integration: [![Open your Home Assistant instance and show an integration.](https://my.home-assistant.io/badges/integration.svg)](https://my.home-assistant.io/redirect/integration/?domain=udi_iox)
2. Click on the hub device (e.g. *IoX-Testing*, your eisy's name).
3. On the hub device page, click **⋮** in the **Device info** card → **Download diagnostics**.

   ![Download diagnostics menu on the hub device card](docs/images/download-diagnostics.png)

4. Attach the `.json` file to the issue.

PII (passwords, portal email, host) is redacted; node addresses and names stay verbatim so triage keeps context.

### 2. Debug log

Required for any state / event / command bug — the integration emits per-event lines at `DEBUG` so we can see which control / property a bad reading came in on. ([HA docs on enabling debug logging](https://www.home-assistant.io/docs/configuration/troubleshooting/#enabling-debug-logging))

1. Open the integration: [![Open your Home Assistant instance and show an integration.](https://my.home-assistant.io/badges/integration.svg)](https://my.home-assistant.io/redirect/integration/?domain=udi_iox)
2. Click **⋮** in the top-right corner of the integration page → **Enable debug logging**.

   ![Enable debug logging menu on the integration page](docs/images/enable-debug-logging.png)

3. **Reproduce the bug** — toggle the broken switch, fire the program, etc.
4. Click **⋮** → **Disable debug logging** — HA automatically downloads the captured log file.
5. Attach the file to the issue.

Debug logging captures every WebSocket frame plus REST request, so keep the reproduction window short — even a few minutes generates several MB.

## Roadmap

Open issues and milestones are tracked in [GitHub Issues](https://github.com/shbatm/hacs-udi-iox/issues).
