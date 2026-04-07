# SmartEVSE Dual Charger

Home Assistant custom integration for two standalone SmartEVSE chargers sharing one feeder.

Version: `0.0.7.4`

This project is for the setup where Home Assistant decides which charger may run, while each SmartEVSE still does its own feeder protection in built-in `Smart` mode.

It is not a SmartEVSE `PWR SHARE` implementation.

## Repository Contents

- Current integration: [`custom_components/smartevse_dual_charger`](custom_components/smartevse_dual_charger)
- Standalone Lovelace card repo: [smartevse-dual-charger-card](https://github.com/bitosome/smartevse-dual-charger-card)
- Upstream reference used for layout exploration: [`references/power-flux-card`](references/power-flux-card)

## Architecture

The integration coordinates both chargers as one controller:

- polls both SmartEVSE devices via `GET /settings`
- writes SmartEVSE state via `POST /settings`
- can push `/currents` and `/ev_meter` to both SmartEVSE devices
- can mirror charger state to one WLED ring
- exposes one Home Assistant control surface for charging policy, force modes, schedule gating, diagnostics, and per-charger state

The integration no longer calculates charging current budgets in Home Assistant.

Instead:

- Home Assistant decides which SmartEVSE is allowed to charge
- the selected SmartEVSE is put into `Smart`
- the other SmartEVSE is put into `Off`
- SmartEVSE itself enforces current limits using its own `Smart`-mode settings and the pushed mains / EV-meter data

That means these SmartEVSE-side settings still matter and must be configured on the chargers themselves:

- `current_main`
- `current_max_circuit`
- `current_min`
- meter/API mode as required by your SmartEVSE setup

## Initial Setup

The config flow asks for:

- optional Vehicle 1 name
- optional Vehicle 2 name
- optional Vehicle 1 battery sensor
- optional Vehicle 2 battery sensor
- optional Vehicle 1 connection-status sensor
- optional Vehicle 2 connection-status sensor
- SmartEVSE 1 base URL/IP
- SmartEVSE 2 base URL/IP
- whether WLED should be set up now
- mains current sensors for L1/L2/L3
- EV-meter current sensors for L1/L2/L3
- EV-meter active power sensor
- EV-meter import/export energy sensors
- price sensor
- schedule entity

Current prefilled defaults:

| Field | Default |
| --- | --- |
| Vehicle 1 name | `Volvo XC40` |
| Vehicle 2 name | `Volvo EX30` |
| Vehicle 1 battery sensor | `sensor.volvo_xc40_battery` |
| Vehicle 1 connection-status sensor | `sensor.volvo_xc40_charging_connection_status` |
| Vehicle 2 battery sensor | `sensor.volvo_ex30_battery` |
| Vehicle 2 connection-status sensor | `sensor.volvo_ex30_charging_connection_status` |
| SmartEVSE 1 base URL/IP | `192.168.0.234` |
| SmartEVSE 2 base URL/IP | `192.168.0.44` |
| WLED URL/IP | `192.168.0.81` |
| Mains L1 | `sensor.shelly_pro_3em_1_phase_a_current` |
| Mains L2 | `sensor.shelly_pro_3em_1_phase_b_current` |
| Mains L3 | `sensor.shelly_pro_3em_1_phase_c_current` |
| EV meter L1 | `sensor.shelly_pro_3em_2_phase_a_current` |
| EV meter L2 | `sensor.shelly_pro_3em_2_phase_b_current` |
| EV meter L3 | `sensor.shelly_pro_3em_2_phase_c_current` |
| EV meter active power | `sensor.shelly_pro_3em_2_total_active_power` |
| EV meter import energy | `sensor.shelly_pro_3em_2_total_active_energy` |
| EV meter export energy | `sensor.shelly_pro_3em_2_total_active_returned_energy` |
| Price sensor | `sensor.real_electricity_price_current_price` |
| Schedule entity | `schedule.charge_schedule` |

Notes:

- SmartEVSE MQTT entities are not required.
- SmartEVSE names are fixed in the integration: `SmartEVSE 1` and `SmartEVSE 2`.
- Vehicle 1 and Vehicle 2 are known vehicle identities used only for EV mapping, battery display, and the connected-EV UI.
- EV battery sensors are optional. If configured, their values are exposed on the controller-state attributes and shown on the EV node in the custom flow card.
- EV connection-status sensors are optional, but they enable EV-to-SmartEVSE identity mapping and let the card show the actual connected vehicle name instead of `?`.
- If WLED setup is enabled, the flow opens a dedicated second WLED step.
- For WLED, enter only the base URL/IP. Do not include `/json/state`.
- Only one config entry is supported.

If WLED setup is enabled, the second step asks for:

- WLED URL/IP
- WLED LED count
- WLED LED offset
- full `presets.json` content to upload

The WLED step validates the JSON and then performs the destructive WLED rebuild before the config entry is created.

## Options Flow

The options flow controls the default behavior of the integration:

- Vehicle 1 name
- Vehicle 2 name
- Vehicle 1 battery sensor
- Vehicle 2 battery sensor
- Vehicle 1 EV connection-status sensor
- Vehicle 2 EV connection-status sensor
- default charge policy
- duty cycle
- controller refresh interval
- mains current push enable + interval
- EV meter push enable + interval
- WLED push enable
- destructive WLED recreation checkbox
- schedule-window notification toggle

The runtime number entities reflect the live values and can be changed directly from Home Assistant without reopening the options dialog.

The options flow does not edit WLED layout fields directly. If WLED is already configured, the recreate checkbox reuses the stored WLED URL/IP, LED count, LED offset, and presets JSON.
Changing Vehicle 1 or Vehicle 2 updates the connected-EV labels after the entry reloads. It does not rename SmartEVSE 1 or SmartEVSE 2.

## Charge Triggers and Precedence

Available charging gates:

- `Force charge`
- `Force charge timer`
- `Force charge by price`
- `Charge with schedule`

Precedence is fixed:

1. `Force charge`
2. `Force charge timer`
3. `Force charge by price`
4. `Charge with schedule`

If `Charge with schedule` is enabled at the same time as `Force charge by price`, the schedule window is treated as an additional gate. Price-based charging will not run outside the schedule window.

Practical result:

- `Force charge` and `Force charge timer` override schedule
- `Force charge by price` can also require the schedule window when schedule charging is enabled
- force modes are mutually exclusive
- `Force charge by price` uses price as an additional gate when schedule charging is also enabled

High-level controller states:

- `idle`
- `force`
- `timer`
- `price`
- `schedule`
- `blocked`

Important trigger behavior:

- timer mode is cleared on Home Assistant restart
- if both EVs are unplugged, all force modes are cleared and the runtime charge policy resets to the configured default
- when charging becomes allowed again after being disallowed, the controller starts a fresh cycle for still-plugged EVs
- enabling a force mode starts a fresh manual cycle

## Charge Policy

Available policies:

- `SmartEVSE 1 first`
- `SmartEVSE 2 first`
- `SmartEVSE 1 only`
- `SmartEVSE 2 only`

Behavior:

- `SmartEVSE 1 first`: if both unfinished EVs are connected, SmartEVSE 1 starts first, then rotation is controlled by duty cycle
- `SmartEVSE 2 first`: same, but SmartEVSE 2 starts first
- `SmartEVSE 1 only`: only SmartEVSE 1 may charge
- `SmartEVSE 2 only`: only SmartEVSE 2 may charge

There are two policy layers:

- configured default policy from the options flow
- runtime `Charge policy` select in Home Assistant

The runtime select is temporary:

- it applies immediately
- it resets the current cycle immediately
- it is cleared back to the configured default when both EVs are unplugged

Changing either the runtime charge policy or duty cycle while charging is active restarts the current cycle immediately.

## Duty Cycle Behavior

`Duty cycle` applies only to the `SmartEVSE 1 first` and `SmartEVSE 2 first` policies, and only while two unfinished connected EV sessions are competing.

If there is only one unfinished connected EV, duty cycle is not used.

### Charging Scenarios

| Scenario | Result |
| --- | --- |
| No EV connected | No charging |
| One unfinished EV connected on either charger | That SmartEVSE charges continuously in `Smart` |
| Two unfinished EVs connected, policy `SmartEVSE 1 first` | SmartEVSE 1 starts, then rotates by duty cycle |
| Two unfinished EVs connected, policy `SmartEVSE 2 first` | SmartEVSE 2 starts, then rotates by duty cycle |
| Policy `SmartEVSE 1 only` | Only SmartEVSE 1 may charge |
| Policy `SmartEVSE 2 only` | Only SmartEVSE 2 may charge |
| Second EV connects while one EV is already charging | Controller reevaluates immediately on the next cycle and starts a fresh policy-based cycle |
| Waiting EV disconnects | Remaining unfinished EV continues without rotation |
| Active EV disconnects | Other unfinished connected EV takes over immediately on the next cycle |
| Active EV finishes before duty cycle ends | Duty cycle for that EV is cancelled and the other unfinished connected EV starts on the next controller cycle |
| Both connected EVs are already complete | Controller goes `blocked` until unplug, manual reset, or a new allowed charging window starts |

How session completion is detected:

- if a mapped EV reports `charging_status = done` / `complete`, that session is considered complete immediately
- otherwise the integration falls back to SmartEVSE-side state transitions and a short non-charging grace period
- unplugging clears completion for that side
- a new charging session clears completion again

## Price and Schedule Handling

Price mode:

- if the price sensor is missing or invalid, charging is blocked
- invalid price data is not treated as `0`
- charging starts only when `price <= acceptable_price`
- if schedule charging is also enabled, the schedule window must be active too

Schedule mode:

- if the schedule entity is missing, charging is blocked
- if the schedule window is active while the schedule gate is disabled, the integration can create a persistent notification

The schedule reminder is implemented as a Home Assistant persistent notification, not as `notify.notify`.

## Controller Refresh and Meter Pushes

The integration has three independent timings:

| Setting | Default | Purpose |
| --- | --- | --- |
| Controller refresh interval | `10 s` | Poll SmartEVSE status, reevaluate charge reason, recompute active SmartEVSE, update entities |
| Mains current push interval | `10 s` | Push `/currents` to both SmartEVSE devices |
| EV meter push interval | `10 s` | Push `/ev_meter` to both SmartEVSE devices |

Important details:

- mains and EV-meter pushes run on dedicated loops, separate from the main controller refresh
- the EV-meter loop is intentionally offset so it does not align with the mains push loop
- interval changes made through Home Assistant number entities apply immediately

Fail-safe behavior:

- if mains current pushing is enabled and any mains phase sensor is invalid, charging is blocked and `controller_error` becomes `mains_data_unavailable`
- the integration does not push `0/0/0` to SmartEVSE in that case
- if SmartEVSE REST becomes unavailable, controller error reflects the unavailable endpoint when applicable

## WLED Integration

When enabled, the integration drives one WLED device directly over the JSON API.

Physical model:

- 105 LEDs
- circular layout
- global LED offset of `11`
- two fixed half-circle segments
- physical result: SmartEVSE 1 on the left, SmartEVSE 2 on the right

Runtime visuals:

- disconnected: off
- connected / ready / charging stopped: blue pulsing idle animation
- charging: green animated
- SmartEVSE 1 charging animation runs in reverse direction
- error: red

### WLED Recreation Checkbox

The setup flow and options flow include this destructive action:

- `Delete all WLED presets and segments, then recreate the SmartEVSE layout and LED map`

When checked, the integration:

- uploads a fresh `ledmap.json`
- deletes all existing WLED segments
- deletes all existing WLED presets
- recreates the two SmartEVSE segments
- recreates presets from the stored `presets.json` content

The flow shows a progress spinner while this runs.

Important: this wipes unrelated presets and segments on that WLED device too.

### Recreated WLED Presets

The recreated preset set is:

- `SmartEVSE Off`
- `SmartEVSE Error`
- `SmartEVSE 1 Charging`
- `SmartEVSE 1 Idle`
- `SmartEVSE 2 Charging`
- `SmartEVSE 2 Idle`
- `SmartEVSE 1 Idle + SmartEVSE 2 Idle`
- `SmartEVSE 1 Charging + SmartEVSE 2 Idle`
- `SmartEVSE 1 Idle + SmartEVSE 2 Charging`
- `SmartEVSE 1 Charging + SmartEVSE 2 Charging`

Presets are recreated mainly as a setup/bootstrap asset set. Runtime control is done by writing WLED segment state directly.

## Home Assistant Entities

### Switches

- `Force charge`
- `Force charge by price`
- `Force charge timer`
- `Charge with schedule`

### Numbers

All numeric inputs use Home Assistant number entities with box input.

- `Acceptable price`
- `Force charge duration`
- `Duty cycle`
- `Controller refresh interval`
- `Mains current push interval`
- `EV meter push interval`

### Selects

- `Charge policy`

### Sensors

- `Controller state`
- `Charge reason`
- `Controller error`
- `Active SmartEVSE`
- `Duty cycle remaining`
- `Timer remaining`
- `SmartEVSE 1 state`
- `SmartEVSE 1 plug state`
- `SmartEVSE 1 mode`
- `SmartEVSE 1 offered current`
- `SmartEVSE 1 max current`
- `SmartEVSE 1 override current`
- `SmartEVSE 1 error`
- `SmartEVSE 1 connected EV`
- `SmartEVSE 2 state`
- `SmartEVSE 2 plug state`
- `SmartEVSE 2 mode`
- `SmartEVSE 2 offered current`
- `SmartEVSE 2 max current`
- `SmartEVSE 2 override current`
- `SmartEVSE 2 error`
- `SmartEVSE 2 connected EV`

The `Controller state` sensor also exposes useful extra attributes such as:

- `charge_allowed`
- `charge_reason`
- `controller_error`
- `active_smartevse`
- `duty_cycle_remaining`
- `charge_policy`
- current intervals
- per-SmartEVSE session completion flags
- last push timestamps

### Service Actions

- `smartevse_dual_charger.refresh`
- `smartevse_dual_charger.reset_sessions`

Behavior:

- `refresh`: runs one controller cycle immediately
- `reset_sessions`: clears remembered per-EV completion state and active SmartEVSE selection, then starts a fresh cycle

These are service actions only. The integration no longer exposes separate button entities for them.

## Controller Error Values

`Controller error` is meant for automation and notification handling. Current actionable values are:

- `mains_data_unavailable`
- `price_sensor_unavailable`
- `schedule_entity_unavailable`
- `smartevse_api_unavailable`
- per-device API failures such as `smartevse_1_api_unavailable` or `smartevse_2_api_unavailable`

## EV Identity Mapping

The integration can map a known EV to each SmartEVSE using the configured connection-status sensors.

Mapping behavior:

- on each controller refresh, the integration watches SmartEVSE plug state and the configured EV connection-status sensors
- when a SmartEVSE changes to connected, it correlates that with which EV connection-status sensor changed to connected
- if the match is unambiguous, that EV is assigned to the SmartEVSE
- unplugging either the SmartEVSE side or the EV-side sensor clears the mapping
- if the match is ambiguous, the mapping stays `unknown`

Exposed mapping surfaces:

- `sensor.smartevse_dual_charger_smartevse_1_connected_ev`
- `sensor.smartevse_dual_charger_smartevse_2_connected_ev`
- matching controller-state attributes used by the standalone card

Current limitation:

- if Home Assistant starts while both EVs are already plugged in and there is no persisted mapping or fresh connection event, the integration may temporarily report `unknown` until the next unambiguous connect/disconnect event

## Dashboard

[`card_integration.yaml`](card_integration.yaml) is the integration-only Mushroom example.

It shows:

- current electricity price
- active SmartEVSE
- duty cycle remaining
- detailed per-SmartEVSE state cards
- schedule control
- force charge, price, and timer controls
- charge policy and main tuning entities

It expects Mushroom cards and reads detailed charger state from the stable `Controller state` sensor attributes, so the card does not depend on per-SmartEVSE entity-ID naming.

The standalone flow card now lives in the separate [smartevse-dual-charger-card](https://github.com/bitosome/smartevse-dual-charger-card) repository. That repo contains:

- the custom `smartevse-flow-card` implementation
- the HACS/frontend artifact
- the visual flow dashboard example
- the local preview page for card layout work

## Legacy Compatibility

Do not run the old YAML automation and this integration at the same time.

Both write SmartEVSE modes and will fight each other.

## Project Layout

- [`custom_components/smartevse_dual_charger/__init__.py`](custom_components/smartevse_dual_charger/__init__.py): setup, unload, service registration
- [`custom_components/smartevse_dual_charger/config_flow.py`](custom_components/smartevse_dual_charger/config_flow.py): initial setup and options flow
- [`custom_components/smartevse_dual_charger/controller.py`](custom_components/smartevse_dual_charger/controller.py): charging logic, SmartEVSE API I/O, session tracking, WLED calls, notifications
- [`custom_components/smartevse_dual_charger/coordinator.py`](custom_components/smartevse_dual_charger/coordinator.py): refresh scheduling, push loops, immediate price/schedule refresh triggers
- [`custom_components/smartevse_dual_charger/number.py`](custom_components/smartevse_dual_charger/number.py): runtime number entities
- [`custom_components/smartevse_dual_charger/select.py`](custom_components/smartevse_dual_charger/select.py): runtime charge policy select
- [`custom_components/smartevse_dual_charger/sensor.py`](custom_components/smartevse_dual_charger/sensor.py): controller and SmartEVSE detail sensors
- [`custom_components/smartevse_dual_charger/switch.py`](custom_components/smartevse_dual_charger/switch.py): force and schedule switches
- [`custom_components/smartevse_dual_charger/wled.py`](custom_components/smartevse_dual_charger/wled.py): WLED runtime control, LED map, segment/preset recreation
- [`custom_components/smartevse_dual_charger/diagnostics.py`](custom_components/smartevse_dual_charger/diagnostics.py): diagnostics with URL/IP redaction
- [`custom_components/smartevse_dual_charger/services.yaml`](custom_components/smartevse_dual_charger/services.yaml): service descriptions

## Best-Practice Notes

Current integration structure follows the Home Assistant config-entry model:

- config-entry based setup
- single config entry
- `ConfigEntry.runtime_data`
- `DataUpdateCoordinator`
- unload/reload support
- translated entities with `has_entity_name`
- diagnostics with URL/IP redaction
- service registration in `async_setup`

Known limitations:

- no automated test suite yet
- no dedicated Repairs flow yet
- no reconfigure flow beyond the standard options flow
