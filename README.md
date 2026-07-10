# SmartEVSE Dual Charger

Home Assistant custom integration for two standalone SmartEVSE chargers sharing one feeder.

Version: `0.0.8.2`

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

## Control Model

SmartEVSE state is the source of truth for charging control.

The controller uses these sources this way:

| Source | Used for | Control authority |
| --- | --- | --- |
| SmartEVSE `/settings` state | availability, plug connected/disconnected, charging/stopped state, current mode, current values, error state | authoritative |
| Mains current sensors | feeder current push and safety gate when current pushing is enabled | authoritative for push safety |
| EV-meter sensors | optional `/ev_meter` push to SmartEVSE | informational input to SmartEVSE |
| Price sensor | acceptable-price gate | authoritative only for price mode |
| Schedule entity | schedule-window gate | authoritative only for schedule modes |
| EV connection-status sensors | known-vehicle identity mapping | fresh hint only |
| Derived EV charging-status sensors | faster completion confirmation and mapping correction | fresh hint only |
| EV battery sensors | dashboard/card display | display only |
| EV-meter active power | local proof that the active EV is really drawing charging power | authoritative for no-power completion |

Important EV telemetry rule:

- EV cloud telemetry never decides that charging is impossible.
- EV cloud telemetry never clears or completes a SmartEVSE session by itself.
- EV cloud telemetry is trusted only while the entity state is recent.
- Current freshness window is `10 minutes`.
- If EV telemetry is stale, `unknown`, or `unavailable`, the controller keeps using SmartEVSE-side behavior and exposes a non-blocking controller warning.
- If SmartEVSE reports `Charging` but the local EV meter stays below `500 W` for at least `180 seconds`, the active session is treated as not drawing real charging power and can be completed/switched without waiting for the duty-cycle end.

## Controller Cycle

Each controller refresh performs this sequence:

1. Poll both SmartEVSE devices through `GET /settings`.
2. Track SmartEVSE state transitions for oscillation detection.
3. Update known-EV mapping from SmartEVSE plug events and fresh EV connection sensors.
4. Update per-SmartEVSE session tracking.
5. Expire force-timer mode when its timer has elapsed.
6. Reset force modes and runtime policy if both EVs are unplugged.
7. Resolve whether charging is allowed from force, price, and schedule gates.
8. Resolve the active SmartEVSE from policy, plug state, completion flags, duty cycle, and handoff state.
9. Write modes: active SmartEVSE gets `Smart`, the other SmartEVSE gets `Off`.
10. Update Home Assistant entities and attributes.
11. Push WLED state if WLED is enabled and the current WLED state does not already match the expected state.

Mains current pushing and EV-meter pushing run in separate loops. They do not wait for the controller refresh cycle.

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

Trigger behavior table:

| Enabled controls | Charging allowed when | Notes |
| --- | --- | --- |
| None | Never | Controller remains `idle`. |
| `Force charge` | Immediately | Ignores schedule and price. |
| `Force charge timer` | Immediately until timer expires | Ignores schedule and price. Timer mode is cleared after expiry. |
| `Force charge by price` | Price sensor value is `<= Acceptable price` | Requires a valid price sensor. |
| `Charge with schedule` | Schedule entity is `on` | Requires a valid schedule entity. |
| `Force charge by price` + `Charge with schedule` | Schedule entity is `on` and price is `<= Acceptable price` | Schedule becomes an additional gate for price charging. |
| Multiple force modes accidentally enabled | First active force mode is kept, the rest are cleared | The controller enforces one active force mode. |

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

The tables below assume charging is allowed by the active trigger gate. If charging is not allowed, both SmartEVSE devices are kept out of charging mode.

#### Base Connection Scenarios

| SmartEVSE 1 | SmartEVSE 2 | Policy | Result |
| --- | --- | --- | --- |
| Disconnected | Disconnected | Any | No active SmartEVSE. Controller reason is `waiting_for_connected_ev` when charging is otherwise allowed. |
| Connected and unfinished | Disconnected | Any first-policy | SmartEVSE 1 charges continuously. Duty cycle is not used. |
| Disconnected | Connected and unfinished | Any first-policy | SmartEVSE 2 charges continuously. Duty cycle is not used. |
| Connected and unfinished | Connected and unfinished | `SmartEVSE 1 first` | SmartEVSE 1 starts, then rotation follows duty cycle. |
| Connected and unfinished | Connected and unfinished | `SmartEVSE 2 first` | SmartEVSE 2 starts, then rotation follows duty cycle. |
| Connected and unfinished | Connected and unfinished | `SmartEVSE 1 only` | Only SmartEVSE 1 may charge. SmartEVSE 2 is ignored until policy changes. |
| Connected and unfinished | Connected and unfinished | `SmartEVSE 2 only` | Only SmartEVSE 2 may charge. SmartEVSE 1 is ignored until policy changes. |
| Connected but session complete | Connected and unfinished | First-policy | SmartEVSE 2 charges continuously. Duty cycle is not used. |
| Connected and unfinished | Connected but session complete | First-policy | SmartEVSE 1 charges continuously. Duty cycle is not used. |
| Connected but session complete | Connected but session complete | Any | Controller goes `blocked` with `all_connected_evs_complete`. |

#### Fixed-Policy Scenarios

| Policy | Selected SmartEVSE state | Other SmartEVSE state | Result |
| --- | --- | --- | --- |
| `SmartEVSE 1 only` | Available, connected, unfinished | Any | SmartEVSE 1 charges. SmartEVSE 2 stays `Off`. |
| `SmartEVSE 2 only` | Available, connected, unfinished | Any | SmartEVSE 2 charges. SmartEVSE 1 stays `Off`. |
| `SmartEVSE 1 only` | Disconnected | Connected or disconnected | Controller blocks with `smartevse_1_only_waiting_for_selected_ev`. |
| `SmartEVSE 2 only` | Disconnected | Connected or disconnected | Controller blocks with `smartevse_2_only_waiting_for_selected_ev`. |
| `SmartEVSE 1 only` | API unavailable | Any | Controller blocks with `smartevse_1_api_unavailable`. |
| `SmartEVSE 2 only` | API unavailable | Any | Controller blocks with `smartevse_2_api_unavailable`. |
| `SmartEVSE 1 only` | Connected but session complete | Any | Controller blocks with `smartevse_1_only_selected_ev_already_complete`. |
| `SmartEVSE 2 only` | Connected but session complete | Any | Controller blocks with `smartevse_2_only_selected_ev_already_complete`. |

#### Connection and Disconnection During Charging

| Event | Result |
| --- | --- |
| Second EV connects while one EV is already charging | Next controller cycle sees two unfinished connected EVs and starts policy-based selection. Duty cycle begins only if both are unfinished. |
| Waiting EV disconnects | Remaining unfinished EV continues continuously. Duty cycle is cancelled because there is no competing EV. |
| Active EV disconnects | Completion state for that SmartEVSE is cleared. The other unfinished connected EV becomes active on the next controller cycle. |
| Both EVs disconnect | Force modes are cleared, runtime charge policy resets to configured default, active SmartEVSE and session tracking are reset. |
| EV reconnects after disconnect | It is treated as a new plug session. Completion state for that SmartEVSE starts fresh. |
| Home Assistant starts while EVs are already plugged in | Charging control still works from SmartEVSE plug state. Known-EV identity may stay `unknown` until a fresh unambiguous EV connection event or charging correction exists. |

#### Completion and Handoff Scenarios

| Scenario | Result |
| --- | --- |
| Active SmartEVSE reaches `Charging`, then later reports connected/ready/stopped/non-charging | Controller starts a non-charging grace timer for that same active turn. |
| Fresh mapped EV charging-status says complete/done while SmartEVSE has stopped after charging | Session is marked complete after at least `max(2 * controller_refresh_interval, 30 seconds)`. |
| EV telemetry is stale/unavailable, or no EV is mapped | Session can still complete, but only from SmartEVSE-side evidence after at least `max(12 * controller_refresh_interval, 180 seconds)`. |
| SmartEVSE says `Charging`, but EV-meter active power stays below `500 W` for `180 seconds` | Session is marked complete from local no-power evidence. This handles a full EV repeatedly closing/opening the contactor while another EV still needs charging. |
| Active EV finishes before duty cycle ends | Current duty-cycle turn is cancelled after completion is confirmed, and the other unfinished connected SmartEVSE becomes active on the next controller cycle. |
| Active SmartEVSE never reaches `Charging` during the current active turn | That SmartEVSE is not marked complete. A failed handoff can fall back to the other eligible SmartEVSE. |
| Duty-cycle handoff target does not reach `Charging` within `max(12 * controller_refresh_interval, 120 seconds)` | Handoff is treated as failed and the other eligible SmartEVSE is selected again. |
| SmartEVSE reports an error while connected | WLED/card show error. Charging selection still follows SmartEVSE availability and controller mode writes; automations should use `Controller error` and per-SmartEVSE error sensors for notifications. |
| User changes runtime policy while charging | Session tracking and active selection are reset immediately, then the new policy is applied. |
| User changes duty cycle while charging | Active selection is reset immediately and the next cycle starts from the current policy. |
| Charging gate turns off, then later turns on | Active selection is reset. When charging becomes allowed again, connected EVs start a fresh cycle. |

#### Blocking and Fail-Safe Scenarios

| Condition | Result |
| --- | --- |
| Both SmartEVSE APIs are unavailable | Charging is blocked with `smartevse_api_unavailable`. |
| One SmartEVSE API is unavailable and policy needs that SmartEVSE | Charging is blocked with the per-device API error. |
| One SmartEVSE API is unavailable but the other connected SmartEVSE is eligible under a first-policy | The available eligible SmartEVSE may charge. |
| Mains current pushing is enabled and any mains phase sensor is invalid | Charging is blocked with `mains_data_unavailable`, and the integration does not push fake zero currents. |
| Price mode is active and price sensor is missing or invalid | Charging is not allowed and `controller_error` is `price_sensor_unavailable`. |
| Schedule mode is active and schedule entity is missing | Charging is not allowed and `controller_error` is `schedule_entity_unavailable`. |
| Schedule is enabled, schedule window is off, and price force is enabled | Charging waits for schedule window before checking acceptable price. |
| SmartEVSE state changes repeatedly while active | Controller exposes an oscillation warning, clears session tracking, and resets active selection. |

How session completion is detected:

- A SmartEVSE session can be marked complete only after that same SmartEVSE has reported `Charging` during the current active turn.
- Fresh EV `charging_status` values such as `complete`, `completed`, `done`, `fully_charged`, or `fully charged` can shorten the grace period.
- EV `idle`, stale EV data, unavailable EV data, or missing EV data does not complete a session.
- Local EV-meter no-power evidence can complete a session even when vehicle-cloud telemetry is stale.
- SmartEVSE states treated as active non-charging evidence are `Connected to EV`, `Ready to Charge`, `Stop Charging`, and states starting with `Charging Stopped`.
- Unplugging clears completion for that SmartEVSE.
- Manual `reset_sessions`, a new allowed charging window, force-mode enablement, policy change, or duty-cycle change starts fresh session tracking.

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

### Combining the schedule with force modes

A common setup is to leave `Charge with schedule` permanently enabled as a "set and forget" safety net for the overnight window, and use the force switches for ad‑hoc daytime charging. Because `Charge with schedule` acts as an additional gate for `Force charge by price` (see the precedence rules above), the combinations behave as follows:

| You want | Do this | Result |
| --- | --- | --- |
| Guaranteed overnight charge (set and forget) | Keep `Charge with schedule` on | Charges every day inside the schedule window, regardless of price. |
| Daytime unconditional top‑up | Turn on `Force charge` | Charges immediately, 24/7, overriding the schedule and price. Does **not** auto‑expire — you must turn it off manually. Enabling it also clears `Force charge by price` / `Force charge timer`. The schedule stays enabled in the background and resumes when you turn `Force charge` off. |
| Daytime charge only when price is acceptable | Turn `Charge with schedule` **off**, then turn `Force charge by price` on | Price charging is gated by the schedule window, so while the schedule is on it never charges outside the window. Turning the schedule off removes the gate and lets price charging run during the day. Re‑enable the schedule afterwards. |
| Overnight, only when price is acceptable | Keep both `Charge with schedule` and `Force charge by price` on | Charges inside the schedule window only while `price <= acceptable_price`. |

The daytime price case has friction: you must temporarily disable `Charge with schedule`, and if you forget to re‑enable it your guaranteed overnight charge is lost. The integration's built‑in persistent notification and the example automation below help catch this.

### Example: reminder automation to re‑enable the schedule

This automation reminds you (via `notify.notify`) one hour before and at the schedule start time when `Charge with schedule` was left off. Adjust the two `at:` times to match your schedule window's start (the example assumes a `00:00` start, so it reminds at `23:00` and `00:00`).

```yaml
- alias: SmartEVSE schedule reminder
  description: Reminds to enable "Charge with schedule" one hour before and at the
    schedule start time if it was forgotten.
  triggers:
  - trigger: time
    at: "23:00:00"
    id: schedule_reminder_1h
  - trigger: time
    at: "00:00:00"
    id: schedule_reminder_now
  conditions:
  - condition: state
    entity_id: switch.smartevse_dual_charger_charge_with_schedule
    state: "off"
  actions:
  - choose:
    - conditions:
      - condition: trigger
        id:
        - schedule_reminder_1h
      sequence:
      - action: notify.notify
        data:
          title: EV charge schedule
          message: >-
            Reminder: charge schedule starts in 1 hour (00:00) but "Charge with
            schedule" is OFF. Enable it if you want the guaranteed overnight charge.
    - conditions:
      - condition: trigger
        id:
        - schedule_reminder_now
      sequence:
      - action: notify.notify
        data:
          title: EV charge schedule
          message: >-
            Charge schedule window has started (00:00) but "Charge with schedule"
            is OFF. Enable it now to charge overnight.
  mode: single
```

Notes:

- The condition only fires the reminders when `Charge with schedule` is off, so you are not notified once it is enabled.
- `notify.notify` targets your default notify service. Replace it with a specific service (for example a mobile app) to reach a particular device.
- This complements the built‑in persistent notification, which fires when the schedule window is already active while the schedule gate is disabled.

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

Runtime update behavior:

- the controller reads current WLED state on every controller refresh
- if WLED already matches the expected two-segment state, no payload is posted
- if WLED differs, the integration posts one `/json/state` payload with both SmartEVSE segments
- runtime control does not activate WLED presets; presets are kept as reusable/bootstrap assets

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
- `vehicle_mapping_ambiguous`
- `smartevse_1_vehicle_data_unavailable`
- `smartevse_2_vehicle_data_unavailable`
- `smartevse_1_vehicle_data_stale`
- `smartevse_2_vehicle_data_stale`
- `smartevse_1_state_oscillation`
- `smartevse_2_state_oscillation`

Vehicle mapping and vehicle data warnings are non-blocking. They are exposed so automations can notify about degraded identity/telemetry quality, but charging control continues from SmartEVSE state.

## EV Identity Mapping

The integration can map a known EV to each SmartEVSE using the configured connection-status sensors.

Mapping behavior:

- on each controller refresh, the integration watches SmartEVSE plug state and the configured EV connection-status sensors
- when a SmartEVSE changes to connected, it correlates that with which EV connection-status sensor changed to connected
- if the match is unambiguous, that EV is assigned to the SmartEVSE
- unplugging the SmartEVSE side clears the mapping
- a fresh EV-side disconnected state clears that EV from any SmartEVSE mapping
- if the match is ambiguous, the mapping stays `unknown`
- if exactly one SmartEVSE and exactly one known EV are freshly observed as charging, the mapping can be corrected from that charging evidence
- stale or unavailable EV telemetry is ignored for mapping decisions

Mapping scenario table:

| Scenario | Mapping result |
| --- | --- |
| One SmartEVSE plug changes to connected and one fresh EV connection sensor changes to connected | That EV is mapped to that SmartEVSE. |
| One SmartEVSE is connected, one fresh known EV is connected, and no other connected candidates exist | That EV is mapped to that SmartEVSE. |
| Both SmartEVSE plugs are connected and both EV sensors are already connected with no fresh event | Mapping may remain `unknown` to avoid guessing. |
| One SmartEVSE reports `Charging` and exactly one fresh known EV reports `charging` | Mapping is corrected to that EV. |
| EV connection sensor becomes fresh `disconnected` | That EV is removed from any SmartEVSE mapping. |
| SmartEVSE plug becomes disconnected | Mapping and session state for that SmartEVSE are cleared. |
| EV connection or charging sensor is stale/unavailable | Mapping is not changed from that EV data. Controller exposes a non-blocking warning if the EV is already mapped. |

Exposed mapping surfaces:

- `sensor.smartevse_dual_charger_smartevse_1_connected_ev`
- `sensor.smartevse_dual_charger_smartevse_2_connected_ev`
- matching controller-state attributes used by the standalone card

Current limitation:

- if Home Assistant starts while both EVs are already plugged in and there is no persisted mapping or fresh connection event, the integration may temporarily report `unknown` until the next unambiguous connect/disconnect event

Vehicle names are known-vehicle names only. They do not rename the SmartEVSE devices. Internally the chargers remain `SmartEVSE 1` and `SmartEVSE 2`.

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
