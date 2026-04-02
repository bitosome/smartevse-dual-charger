# SmartEVSE Dual Charger

Home Assistant custom integration for two standalone SmartEVSE chargers sharing one feeder.

This repository contains:

- The current integration in [`custom_components/smartevse_dual_charger`](custom_components/smartevse_dual_charger)
- The legacy YAML automation in [`automation.yaml`](automation.yaml)
- The legacy helper-based dashboard in [`card.yaml`](card.yaml)
- The current integration dashboard example in [`card_integration.yaml`](card_integration.yaml)

## What It Does

The integration acts as a single coordinator for both SmartEVSE devices:

- Reads each SmartEVSE directly from `GET /settings`
- Controls each SmartEVSE directly through `POST /settings`
- Pushes `/currents` and `/ev_meter` updates to both SmartEVSE devices when enabled
- Lets SmartEVSE handle feeder protection in built-in `Smart` mode
- Alternates charging access between SmartEVSE 1 and SmartEVSE 2 on a configurable duty cycle
- Stops using duty-cycle rotation as soon as only one unfinished EV session remains
- Exposes controller entities for force charge, schedule gating, pricing, charge policy, and diagnostics
- Mirrors charger state to WLED when configured
- Can recreate SmartEVSE-specific WLED presets and LED mapping from the options flow

This integration is designed for the "two independent SmartEVSE chargers, one shared supply, SmartEVSE owns the current regulation" setup. It is not a `PWR SHARE` implementation.

## Control Model

The integration no longer calculates current budgets or target currents in Home Assistant.

Instead:

- Home Assistant decides which SmartEVSE is allowed to charge right now
- The selected SmartEVSE is put in `Smart` mode
- The non-selected SmartEVSE is put in `Off`
- SmartEVSE uses its own configured `Smart`-mode logic together with the pushed mains and EV-meter data

That means feeder protection now depends on the SmartEVSE configuration itself:

- `current_main`
- `current_max_circuit`
- `current_min`
- SmartEVSE meter mode being set to API where required

Those values must be configured on the SmartEVSE devices themselves. They are not derived by Home Assistant anymore.

## Charge Policy

`Charge policy` replaces the old low-budget policy.

Available policies:

- `SmartEVSE 1 first`
- `SmartEVSE 2 first`
- `SmartEVSE 1 only`
- `SmartEVSE 2 only`

Behavior:

- `SmartEVSE 1 first`: when both unfinished EV sessions are connected, SmartEVSE 1 gets the first duty-cycle slot, then charging alternates between both chargers
- `SmartEVSE 2 first`: same, but SmartEVSE 2 starts first
- `SmartEVSE 1 only`: only SmartEVSE 1 is ever enabled; duty-cycle rotation is not used
- `SmartEVSE 2 only`: only SmartEVSE 2 is ever enabled; duty-cycle rotation is not used

If only one unfinished EV session is connected and the policy is one of the `first` policies, that SmartEVSE charges immediately and duty-cycle timing is not used. If a second unfinished EV becomes connected later, the controller starts a fresh duty-cycle window from that point and reapplies the preferred `first` slot immediately.

The configured default policy is set in the integration options and defaults to `SmartEVSE 1 first`. The Home Assistant `Charge policy` select is a temporary runtime override for the current plugged-in session only. When both EVs are unplugged, the runtime policy automatically resets back to the configured default.

The same charge policy is applied regardless of why charging is active:

- force charge
- force timer
- force by price
- schedule charging

If the active charge reason changes while charging remains allowed, the duty-cycle state is reset and the newly active mode starts again from the selected charge policy.

## Duty Cycle

`Duty cycle` is the time each SmartEVSE keeps the charging slot while both unfinished EV sessions remain connected under a `first` policy.

Example:

- policy = `SmartEVSE 1 first`
- duty cycle = `60 min`

Result:

1. SmartEVSE 1 enters `Smart`
2. SmartEVSE 2 stays `Off`
3. After 60 minutes, SmartEVSE 1 is turned `Off`
4. SmartEVSE 2 enters `Smart`
5. The cycle repeats while charging is still allowed and both cars remain connected

Important exceptions:

- If the active EV finishes before the duty cycle ends and the other EV is still waiting, the controller switches immediately to the next unfinished EV and does not wait for the slot timer to expire
- If only one unfinished EV session remains connected, the controller keeps that SmartEVSE in `Smart` continuously and does not rotate
- If both connected EV sessions are already complete, the controller blocks further charging until one EV is unplugged or a new charge cycle is started

Changing the runtime `Charge policy` select or `Duty cycle` number takes effect immediately and resets the current rotation so the new policy is applied right away.

## Charge Triggers

The original control scenarios are still covered:

- Manual force charge
- Price-gated charging
- Timer-based charging with automatic expiry
- Schedule-gated charging
- Mutual exclusion between force modes
- Timer reset on Home Assistant restart
- Force-mode reset when both cars are unplugged
- Schedule reminder when a schedule window starts while the schedule gate is disabled
- Periodic SmartEVSE `/currents` and `/ev_meter` pushes
- WLED state mirroring for both chargers

One implementation detail differs from the legacy YAML:

- The reminder is sent as a Home Assistant persistent notification instead of `notify.notify`

Do not run the legacy automation and this integration at the same time. Both will write SmartEVSE modes and will fight each other.

## Meter Push Cadence

With SmartEVSE `Smart` mode, the important reaction time is the mains and EV-meter push cadence, not the Home Assistant slot decision loop.

The integration now sends meter data on dedicated timers, separate from the main controller refresh:

- `Mains current push interval`
- `EV meter push interval`
- `Controller refresh interval` only affects SmartEVSE status polling, schedule/price reevaluation, and duty-cycle switching

This matches the old `rest:` behavior more closely and avoids current pushes being delayed by slow `GET /settings` calls. The EV-meter loop is intentionally offset from the mains-current loop so both push jobs do not hit the SmartEVSE web server at the same second. If breaker protection needs faster reaction, lower the push intervals.

Current defaults:

- `Controller refresh interval`: `10 s`
- `Mains current push interval`: `10 s`
- `EV meter push interval`: `10 s`

All three intervals are exposed in two places:

- Integration options
- Home Assistant number entities for live runtime tuning

## WLED Layout

When WLED is enabled, the integration drives a 105-LED circular layout with a 10-LED start offset and two fixed half-circle segments:

- Left half: SmartEVSE 1
- Right half: SmartEVSE 2

The integration applies that geometry through `ledmap.json`, so the runtime WLED payload can still use only two contiguous segments.

State colors:

- Charging: green animated, with SmartEVSE 1 running in reverse direction
- Connected, `Ready to Charge`, and `Charging Stopped`: the same blue pulsing idle animation
- Error: red

## WLED Presets

The options flow includes a one-shot checkbox:

- `Delete old SmartEVSE WLED presets and recreate the segment layout and LED map`

When checked, the integration:

- Deletes existing WLED segments beyond the SmartEVSE pair and rebuilds the two fixed SmartEVSE half-circle segments
- Uploads a fresh `ledmap.json` for the circular 105-LED layout
- Removes old WLED presets whose names start with `SmartEVSE`
- Creates a new namespaced SmartEVSE preset set without touching unrelated user presets

The recreated preset set includes combined two-segment presets for the practical shared-ring states, for example:

- `SmartEVSE 1 Idle + SmartEVSE 2 Idle`
- `SmartEVSE 1 Charging + SmartEVSE 2 Idle`
- `SmartEVSE 1 Idle + SmartEVSE 2 Charging`
- `SmartEVSE 1 Charging + SmartEVSE 2 Charging`

The options dialog shows a progress spinner while that work runs.

## Default Configuration Values

The initial config flow is prefilled with the original installation defaults:

- SmartEVSE 1 base URL/IP: `192.168.0.234`
- SmartEVSE 2 base URL/IP: `192.168.0.44`
- WLED URL/IP: `192.168.0.81`
- Shelly 3EM mains sensors
- Shelly 3EM EV-meter sensors
- Price sensor
- Schedule entity

The SmartEVSE MQTT entities are no longer required in the config flow because the integration reads and writes the SmartEVSE devices directly through REST.

## Entities

The integration creates:

- Switches: force charge, force charge by price, force charge timer, charge with schedule
- Numbers: acceptable price, duty cycle, controller refresh interval, mains current push interval, EV meter push interval
- Text: force charge duration (`H:MM`, for example `3:32`)
- Selects: charge policy
- Sensors:
  - Controller state and charge reason
  - Active charge slot
  - Duty cycle remaining
  - SmartEVSE 1 state, plug state, mode, charging current, max current, override current, error
  - SmartEVSE 2 state, plug state, mode, charging current, max current, override current, error
  - Timer remaining

Service actions:

- `smartevse_dual_charger.refresh`
- `smartevse_dual_charger.reset_sessions`

Behavior:

- `Refresh controller` / `smartevse_dual_charger.refresh`: runs one controller cycle immediately. This forces an immediate SmartEVSE status poll, reevaluates force/schedule/price/timer state, reapplies the current charge policy if needed, and refreshes the integration entities. It does not reset timers, force modes, or duty-cycle state.
- `Reset charge cycle` / `smartevse_dual_charger.reset_sessions`: clears the integration's active slot rotation state, forgets remembered per-EV completion state, and immediately reevaluates the current charge policy from a clean start.

## Dashboard

[`card_integration.yaml`](card_integration.yaml) is the current dashboard example.

It shows:

- Controller state, charge reason, active slot, and duty-cycle remaining
- Per-SmartEVSE state, plug state, mode, charging current, max current, override current, slot status, and error
- Schedule, force-charge, price-gated, and timer controls
- Charge policy, duty cycle, pricing, and schedule settings

[`card.yaml`](card.yaml) is retained only as a reference for the old helper-based setup.

## Project Layout

- [`custom_components/smartevse_dual_charger/__init__.py`](custom_components/smartevse_dual_charger/__init__.py): integration setup, service registration, config-entry wiring
- [`custom_components/smartevse_dual_charger/config_flow.py`](custom_components/smartevse_dual_charger/config_flow.py): initial setup and options flow
- [`custom_components/smartevse_dual_charger/controller.py`](custom_components/smartevse_dual_charger/controller.py): charge policy orchestration, timer/force logic, SmartEVSE API I/O, WLED, notifications
- [`custom_components/smartevse_dual_charger/coordinator.py`](custom_components/smartevse_dual_charger/coordinator.py): scheduled refresh and immediate price/schedule refresh triggers
- [`custom_components/smartevse_dual_charger/sensor.py`](custom_components/smartevse_dual_charger/sensor.py): controller and SmartEVSE detail sensors
- [`custom_components/smartevse_dual_charger/services.yaml`](custom_components/smartevse_dual_charger/services.yaml): service action descriptions

## Best-Practice Notes

The current refactor keeps the integration aligned with the Home Assistant config-entry model:

- Uses config entries and `ConfigEntry.runtime_data`
- Uses `DataUpdateCoordinator`
- Registers service actions in `async_setup`
- Supports unload/reload
- Uses translated entity names and `has_entity_name`
- Exposes diagnostics with URL/IP redaction
- Uses a service device entry for the controller
- Logs endpoint failures only on transition instead of every cycle

Still worth improving:

- Automated tests are still missing
- The config flow validates URL/IP shape but does not yet verify connectivity
- A dedicated reconfigure flow is not implemented yet
- There is still no repair flow for broken external entity mappings or unreachable devices

## Validation

Validate locally after changes with:

- `python3 -m compileall custom_components/smartevse_dual_charger`
- JSON parsing of [`custom_components/smartevse_dual_charger/manifest.json`](custom_components/smartevse_dual_charger/manifest.json)
- JSON parsing of [`custom_components/smartevse_dual_charger/translations/en.json`](custom_components/smartevse_dual_charger/translations/en.json)
- YAML parsing of [`card_integration.yaml`](card_integration.yaml)

Automated regression coverage has not been added yet.
