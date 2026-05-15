# Co-existing with the core `isy994` integration

The two integrations register distinct HA domains (`isy994` and `udi_iox`) and run side-by-side. Common reasons to keep both: an ISY-994 + eisy mixed setup, or a transition period before deleting the core entry. This page summarizes how the same controller looks through each integration.

> **TL;DR:** when both integrations point at the same eisy, **the same physical devices land on the same HA platforms**. The differences are intentional improvements (cleaner naming, dynamic classification) and additions on the `udi_iox` side that core doesn't ship — programs as devices, native event entities, IoX configurables as numeric / select entities.

> **If you're switching from core to this integration:** while the two coexist fine, every entity the same controller exposes through both will collide on `entity_id` — the second integration to register gets a `_2` suffix (`light.kitchen_main` → `light.kitchen_main_2`). To keep dashboards and automations intact, **remove the `isy994` integration first**, restart HA once (the HACS install needs the restart anyway), then add this one. Any `entity_id` you'd manually renamed under core has to be renamed again on the `udi_iox` side.

> **Z-Wave caveat:** the side-by-side audit was run against an Insteon-heavy hub with only a handful of Z-Wave switches and outlets — Z-Wave behaviour has been *spot-checked*, not exhaustively compared. Z-Wave binary sensors (smoke / leak / motion / battery alarms) are routed via the controller-published `devtype.cat` generic-class id, but that path hasn't been exercised against a real Z-Wave sensor on the audit hubs. **If you have Z-Wave devices and notice anything that doesn't match core's behavior, please [open an issue](https://github.com/shbatm/hacs-udi-iox/issues) — feedback from real Z-Wave-heavy setups is what fills the gap.**

## Mostly identical

Audited on a real eisy (~700 entities on core, ~1800 on udi_iox — the extras are programs surfaced as devices, IoX configurables exposed as `number` / `select`, and per-button `event` entities). Of every entity that both integrations surface for the **same controller node**:

- **Platform classification matches 100 %.** A device that core puts on `light` shows up on `light` here too; same for `switch`, `cover`, `lock`, `climate`, `fan`, `sensor`, `binary_sensor`. Zero divergence.
- **Areas track 1:1.** Once you've assigned a device to a HA area, both integrations see the same `area_id` for shared devices.
- **Group / scene counts match.** Both register the same IoX groups as `switch` entities.
- **Insteon node addressing is identical** (`<MAC>_<addr>` style), so HA-registry collisions follow the predictable `_2` suffix rule when both run together.

## Intentional improvements over core

### Cleaner names through translation keys
- Core: `sensor.zw_002_dimmer_switch_zw_002_dimmer_switch_device_communication_errors`
- This integration: `sensor.zw_002_dimmer_switch_responding`

The shorter name comes from a `translation_key`, so it localizes. Same data, less mouthful, future-proof against name churn on the controller.

### Group naming that doesn't smash multi-controller scenes
- Core (multi-controller scene): `switch.driveway_kp_a_breezeway_i_garage_lights` (concatenates every controller's name)
- This integration: `switch.skynet_isy_garage_lights` (falls back to the hub when the scene has more than one controller; attaches single-controller scenes to that controller's device card)

### Sub-node index dropped from light entity_id where it adds nothing
- Core: `light.usb_adapter_0`
- This integration: `light.usb_adapter`

### Dynamic classification (not hardcoded)
Core ships several hardcoded type-prefix tables (Insteon `device categories`, Z-Wave generic-class IDs, etc.) to decide where each device lands. Adding a new device class means editing the integration. This integration reads each device's nodedef from the controller — for Insteon you get the same baseline behavior, but Z-Wave, Zigbee, Matter, and any PG3 plugin classify themselves automatically with no integration update needed.

### Config entry title shows the host
- Core: `Skynet ISY (eisy.iot.bond.casa)`
- This integration: matches — `Skynet ISY (eisy.iot.bond.casa)`. The host is in the title so a changed IP / hostname is visible from the integration card without opening the entry.

## Net additions (not in core)

### IoX programs as devices
Each program (and program folder) gets:
- `Run` / `Run Then` / `Run Else` / `Stop` buttons
- Enable switch + *Run at startup* toggle
- `Last Run`, `Last Finished`, `Next Scheduled Run` timestamp sensors
- A `binary_sensor` exposing the program's last evaluation result

### Native `event` entities for sub-button presses
Core surfaces KeypadLinc / SwitchLinc sub-buttons as `sensor.<keypad>_<letter>` entities whose state value is the last command code. This integration replaces those with `event.*` entities exposed through HA's device-trigger UI — no `udi_iox_control` bus events, no listening for raw control codes. Fires on every press, including same-type repeats. ([Migration guide](#migration-from-cores-keypad-sensors).)

For sensors that have *both* a stateful binary surface (leak / motion / opening) **and** sub-button transitions, both are kept: `binary_sensor.kitchen_sink_leak_dry` (state) **and** `event.kitchen_sink_leak_dry_kitchen_sink_leak_wet` (transition).

### IoX configurables as native HA entities
`number` / `select` / `switch` entities for On Level, Ramp Rate, Backlight, Z-Wave configuration parameters, and anything else the controller publishes as an editor — no service calls, no YAML.

### WebSocket health surface
- `system_health` panel exposes `host_reachable`, `device_connected`, `event_stream_status`, `last_event_at`
- Entities flip to `unavailable` when the WS drops, recover on reconnect

### Repair-card lifecycle UX
New / removed / renamed devices on the controller surface as Repair cards prompting a reload, instead of being silently missed.

## Migration from core's keypad sensors

If you have automations that trigger on `state_changed: sensor.<keypad>_b` (the legacy integer-state pattern), migrate to the **device trigger** UI for the equivalent `event.<keypad>_b` entity. Settings → Automations & Scenes → Create Automation → When → Add trigger → Device, pick the keypad, and choose e.g. *"… was switched On"*. See the README's [Automations on button presses](../README.md#automations-on-button-presses) section for the YAML equivalent.
