# Supported devices

This integration surfaces every device on your eisy / Polisy IoX controller — Insteon, Z-Wave, Zigbee, Matter, and any [PG3 node-server plugin](https://www.universal-devices.com/polyglot/) you have installed. What HA platform each device lands on is driven by `pyisyox`'s classifier reading the nodedef the controller publishes, not a hard-coded device table.

Currently supported HA platforms:

`binary_sensor`, `button`, `climate`, `cover`, `event`, `fan`, `light`, `lock`, `number`, `select`, `sensor`, `switch`

Roadmap: `alarm_control_panel` (Elk M1, Total Connect, DSC PG3 plugins) and `valve` (Rachio / LinkTap irrigation). Both are tracked in [issue #8](https://github.com/shbatm/hacs-udi-iox/issues/8).

## Devices Home Assistant already supports natively

If you bridge a device through a PG3 node-server plugin **and** Home Assistant has a first-party integration for that same device, **install HA's native integration directly.** It will almost always be the better source of truth — local-push state, manufacturer-aware quirks, faster updates, and more device-specific features. The two integrations coexist fine: `udi_iox` still exposes the device's IoX-side state (useful if you have IoX programs or scenes that reference the device), and the native integration handles the device itself.

The PG3 plugins below all have strong HA native integrations. Prefer the native one:

| PG3 plugin | HA native integration |
|---|---|
| `udi-sonos-poly` | [`sonos`](https://www.home-assistant.io/integrations/sonos/) |
| `udi-onkyo-poly`, `udi-onkyoavr-poly` | [`onkyo`](https://www.home-assistant.io/integrations/onkyo/) |
| `udi-plex-poly` | [`plex`](https://www.home-assistant.io/integrations/plex/) |
| `udi-roku-poly` | [`roku`](https://www.home-assistant.io/integrations/roku/) |
| `udi-volumio-poly` | [`volumio`](https://www.home-assistant.io/integrations/volumio/) |
| `udi-russound-poly` | [`russound_rio`](https://www.home-assistant.io/integrations/russound_rio/) |
| `udi-rachio-poly` | [`rachio`](https://www.home-assistant.io/integrations/rachio/) |
| `udi-poly-hue-emu` | [`hue`](https://www.home-assistant.io/integrations/hue/) |
| `udi-poly-ecobee` | [`ecobee`](https://www.home-assistant.io/integrations/ecobee/) |
| `udi-honeywellhome-poly` | [`honeywell`](https://www.home-assistant.io/integrations/honeywell/) |
| `udi-poly-kasa` | [`tplink`](https://www.home-assistant.io/integrations/tplink/) |
| `udi-shelly-poly` | [`shelly`](https://www.home-assistant.io/integrations/shelly/) |
| `udi-nanoleaf-polyglot` | [`nanoleaf`](https://www.home-assistant.io/integrations/nanoleaf/) |
| `udi-twinkly-nodeserver` | [`twinkly`](https://www.home-assistant.io/integrations/twinkly/) |
| `udi-wled-nodeserver` | [`wled`](https://www.home-assistant.io/integrations/wled/) |
| `udi-govee-nodeserver` | [`govee_light_local`](https://www.home-assistant.io/integrations/govee_light_local/) |
| `udi-sensibo-poly` | [`sensibo`](https://www.home-assistant.io/integrations/sensibo/) |
| `udi-flair-polyglot` | [`flair`](https://www.home-assistant.io/integrations/flair/) |
| `udi-august-nodeserver` | [`august`](https://www.home-assistant.io/integrations/august/) |
| `udi-wemo-poly` | [`wemo`](https://www.home-assistant.io/integrations/wemo/) |
| `udi-solaredge-poly` | [`solaredge`](https://www.home-assistant.io/integrations/solaredge/) |
| `udi-poly-FlumeWater` | [`flume`](https://www.home-assistant.io/integrations/flume/) |
| `udi-sense-monitoring-polyglot` | [`sense`](https://www.home-assistant.io/integrations/sense/) |
| `udi-netatmo` | [`netatmo`](https://www.home-assistant.io/integrations/netatmo/) |
| `udi-poly-Airthings-Consumer` | [`airthings`](https://www.home-assistant.io/integrations/airthings/) |
| `udi-aeris-poly`, `udi-weatherflow-poly`, `udi-noaa-poly` | [`nws`](https://www.home-assistant.io/integrations/nws/), [`met`](https://www.home-assistant.io/integrations/met/), [`weatherflow_cloud`](https://www.home-assistant.io/integrations/weatherflow_cloud/) |
| `udi-purpleair-poly` | [`purpleair`](https://www.home-assistant.io/integrations/purpleair/) |
| `udi-emporia-vue-poly` | `emporia_vue` (HACS) |
| `udi-presenceUnifi-nodeserver` | [`unifi`](https://www.home-assistant.io/integrations/unifi/) |
| `udi-roomba-poly` | [`roomba`](https://www.home-assistant.io/integrations/roomba/) |
| `udi-harmony-poly` | [`harmony`](https://www.home-assistant.io/integrations/harmony/) |
| `udi-push-poly` | [`pushover`](https://www.home-assistant.io/integrations/pushover/) |
| `holidays-poly`, `holidays-google-poly` | [`workday`](https://www.home-assistant.io/integrations/workday/), [`holiday`](https://www.home-assistant.io/integrations/holiday/), [`google`](https://www.home-assistant.io/integrations/google/) |

## Devices where the IoX path is the right call

| PG3 plugin / native device | Why IoX |
|---|---|
| Elk M1, Total Connect, DSC (PG3 alarm plugins) | First-party `UDIELKWebServices` on the eisy; no single HA native covers all three vendors. Roadmap: `alarm_control_panel` ([#8](https://github.com/shbatm/hacs-udi-iox/issues/8)). |
| `udi-poly-Camect`, `udi-blue-iris-poly` | Blue Iris has only community integrations; Camect has no HA native. |
| Native Insteon / Z-Wave / Zigbee / Matter on the controller | Surfaced directly through the controller's own radios — no PG3 plugin involved. |
| Any device referenced by IoX programs or scenes | IoX programs only see IoX-side state. If your automations live on the controller, the IoX surface needs the entity. |

## Filing a request

If you're using a PG3 plugin and your device lands on the wrong HA platform — or doesn't show up at all — [open an issue](https://github.com/shbatm/hacs-udi-iox/issues/new) with the nodedef id (`UDD_…` / vendor-specific) and a description. The classifier is driven by the nodedef's `accepts` / `sends` / properties, so a misclassification almost always comes down to either (a) a missing classifier rule (file in [pyisyox](https://github.com/shbatm/pyisyox)), or (b) an override worth shipping in this repo (tracked under [#6](https://github.com/shbatm/hacs-udi-iox/issues/6)).
