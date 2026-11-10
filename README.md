# SmartEVSE Dual Charger

Home Assistant custom integration for two standalone SmartEVSE chargers sharing one feeder.

Version: `0.0.9.0`

This project is for the setup where Home Assistant decides which charger may run, while each SmartEVSE still does its own feeder protection in built-in `Smart` mode.

It is not a SmartEVSE `PWR SHARE` implementation.

Since 0.0.9.0 it can also plan the charging itself: [Intelligent charging](#intelligent-charging)
charges each connected car in the cheapest hours that still reach its minimum charge by its
ready-by time, using a model it learns locally from your chargers, cars and house.

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
- exposes one Home Assistant control surface for charging policy, force modes, schedule gating, Intelligent charging, diagnostics, and per-charger state

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
| Real Electricity Price hourly price lists | Intelligent charging plan prices | planning input for Intelligent charging only |
| Schedule entity | schedule-window gate | authoritative only for schedule modes |
| EV connection-status sensors | known-vehicle identity mapping | fresh hint only |
| Derived EV charging-status sensors | faster completion confirmation and mapping correction | fresh hint only |
| EV battery sensors | dashboard/card display, Intelligent charging energy need | display, and planning input for Intelligent charging |
| EV-meter active power | local proof that the active EV is really drawing charging power | authoritative for no-power completion |

Important EV telemetry rule:

- EV cloud telemetry never decides that charging is impossible.
- EV cloud telemetry never clears or completes a SmartEVSE session by itself.
- EV cloud telemetry is trusted only while the entity state is recent.
- Current freshness window is `10 minutes`.
- If EV telemetry is stale, `unknown`, or `unavailable`, the controller keeps using SmartEVSE-side behavior and exposes a non-blocking controller warning.
- If SmartEVSE reports `Charging` but the local EV meter stays below `500 W` for at least `180 seconds`, the active session is treated as not drawing real charging power and can be completed/switched without waiting for the duty-cycle end.
- Intelligent charging uses the battery level to plan: a car at or above its minimum gets no planned charge, but it is not marked complete from that reading. A battery-level rise seen while that car's charger was not charging never counts as "reached".

## Controller Cycle

Each controller refresh performs this sequence:

1. Poll both SmartEVSE devices through `GET /settings`.
2. Track SmartEVSE state transitions for oscillation detection.
3. Update known-EV mapping from SmartEVSE plug events and fresh EV connection sensors.
4. Update per-SmartEVSE session tracking.
5. Expire force-timer mode when its timer has elapsed.
6. Reset force modes and runtime policy if both EVs are unplugged.
7. Update the Intelligent charging model (in every mode) and, when Intelligent charging is on, re-plan.
8. Resolve whether charging is allowed from force, price, Intelligent charging, and schedule gates.
9. Resolve the active SmartEVSE from policy, plug state, completion flags, duty cycle, handoff state, or the Intelligent plan.
10. Write modes: active SmartEVSE gets `Smart`, the other SmartEVSE gets `Off` (outgoing charger first on a hand-off).
11. Update Home Assistant entities and attributes.
12. Push WLED state if WLED is enabled and the current WLED state does not already match the expected state.

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
- Vehicle 1 and Vehicle 2 are known vehicle identities used for EV mapping, battery display, the connected-EV UI, and their own Intelligent charging settings.
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
- Vehicle 1 / Vehicle 2 battery capacity in kWh for Intelligent charging (`0` = automatic: the Volvo battery-capacity sensor, else 75 kWh; otherwise 10–300 kWh)
- default charge policy
- duty cycle
- controller refresh interval
- mains current push enable + interval
- EV meter push enable + interval
- WLED push enable
- destructive WLED recreation checkbox
- schedule-window notification toggle, optional notify action/targets, and advance lead time

The runtime number entities reflect the live values and can be changed directly from Home Assistant without reopening the options dialog.

The options flow does not edit WLED layout fields directly. If WLED is already configured, the recreate checkbox reuses the stored WLED URL/IP, LED count, LED offset, and presets JSON.
Changing Vehicle 1 or Vehicle 2 updates the connected-EV labels after the entry reloads. It does not rename SmartEVSE 1 or SmartEVSE 2.

## Charge Triggers and Precedence

Available charging gates:

- `Force charge`
- `Force charge timer`
- `Force charge by price`
- `Charge with schedule`
- `Schedule acceptable price`
- `Intelligent charging`, with its optional `Intelligent top-up`

`Force charge` is the global activation switch for the force plan. `Force charge timer`
and `Force charge by price` are optional constraints on that plan. The schedule has
its own independent `Schedule acceptable price` option. `Intelligent charging` is a
standing plan of its own; see [Intelligent charging](#intelligent-charging).

Precedence is fixed:

1. Active `Force charge`, with its selected timer and/or price constraints
2. `Force charge timer` or `Force charge by price` switched on without `Force charge`
   (the pre-0.0.8.6 direct behaviour, kept for older automations)
3. `Intelligent charging`
4. `Charge with schedule`, with its independent price constraint

The schedule and Intelligent switches remain enabled while Force charge is active, but
they are not evaluated until Force charge ends. While `Intelligent charging` is on, the
schedule is not evaluated at all. The two are independent: turning one on or off never
changes the other's switch, so a schedule that is still on takes over again when
Intelligent charging is turned off.

Practical result:

- every active Force charge variant overrides Intelligent charging and the schedule
- `Force charge timer` and `Force charge by price` can be combined for a time-limited, price-gated charge
- Force charge options remain independent from the schedule's acceptable-price option
- Intelligent charging outranks the schedule without turning it off; schedule-disabled reminders are not sent while it is on

High-level controller states:

- `idle`
- `force`
- `timer`
- `price`
- `intelligent`
- `schedule`
- `blocked`

Trigger behavior table:

| Enabled controls | Charging allowed when | Notes |
| --- | --- | --- |
| None | Never | Controller remains `idle`. |
| `Force charge` | Immediately | Ignores schedule when no force options are enabled. |
| `Force charge` + `Force charge timer` | Immediately until timer expires | Ignores schedule. The force plan is cleared after expiry. |
| `Force charge` + timer + price | Until the timer expires, and only while price is `<= Acceptable price` | Ignores schedule. The force plan is cleared when the timer expires. |
| `Force charge` + `Force charge by price` | Price sensor value is `<= Acceptable price` | Ignores schedule and requires a valid price sensor. |
| `Charge with schedule` | Schedule entity is `on` | Requires a valid schedule entity. |
| `Charge with schedule` + `Schedule acceptable price` | Schedule entity is `on` and price is `<= Acceptable price` | Both standing schedule conditions must be satisfied. |
| Force plan + Schedule plan | The selected Force constraints | Schedule remains on and resumes after Force charge ends. |
| `Intelligent charging` | In the planned 15-minute units: the cheapest plan that brings each connected car to its minimum by its ready-by time | Schedule is not evaluated. The planned charger gets `Smart`, the other `Off`. |
| `Intelligent charging` + `Intelligent top-up` | The planned units, plus known-price units at or below `Intelligent top-up price` for cars that already have their minimum | Top-up stops at the car's own charge limit and never runs past the ready-by time. |
| Force plan + `Intelligent charging` | The selected Force constraints | Intelligent status is `overridden`; the plan resumes after Force charge ends. |
| `Intelligent charging` + `Charge with schedule` | The Intelligent plan | The schedule stays on but is not evaluated. |

Important trigger behavior:

- timer mode is cleared on Home Assistant restart
- when a timed force plan expires, its activation and force options are cleared together
- if both EVs are unplugged, all force modes are cleared and the runtime charge policy resets to the configured default
- when charging becomes allowed again after being disallowed, the controller starts a fresh cycle for still-plugged EVs; Intelligent charging keeps completed sessions complete between its planned blocks
- enabling a force mode starts a fresh manual cycle
- `Intelligent charging` and its settings survive unplugging and restarts; per-car plan state (deadline, catch-up, not-accepting) is cleared when that car is unplugged

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
| Force charge with acceptable price is enabled outside the schedule window | Charging ignores the schedule and waits only for an acceptable price. |
| SmartEVSE state changes repeatedly while active | Controller exposes an oscillation warning, clears session tracking, and resets active selection. |

How session completion is detected:

- A SmartEVSE session can be marked complete only after that same SmartEVSE has reported `Charging` during the current active turn.
- Fresh EV `charging_status` values such as `complete`, `completed`, `done`, `fully_charged`, or `fully charged` can shorten the grace period.
- EV `idle`, stale EV data, unavailable EV data, or missing EV data does not complete a session.
- Local EV-meter no-power evidence can complete a session even when vehicle-cloud telemetry is stale.
- SmartEVSE states treated as active non-charging evidence are `Connected to EV`, `Ready to Charge`, `Stop Charging`, and states starting with `Charging Stopped`.
- Unplugging clears completion for that SmartEVSE.
- Manual `reset_sessions`, a new allowed charging window, force-mode enablement, policy change, or duty-cycle change starts fresh session tracking. The start of an Intelligent charging block is not a new window: a completed session stays complete.

## Price and Schedule Handling

Price mode:

- if the price sensor is missing or invalid, charging is blocked
- invalid price data is not treated as `0`
- charging starts only when `price <= acceptable_price`
- if schedule charging is also enabled, the schedule window must be active too

Schedule mode:

- if the schedule entity is missing, charging is blocked
- if the schedule window is active while the schedule gate is disabled, the integration can create a persistent notification

The built-in persistent schedule warning remains enabled by default. Optional notification delivery and an advance reminder are configured in the integration options; no separate automation is needed.

### Combining the schedule with force modes

A common setup is to leave `Charge with schedule` permanently enabled as a "set and forget" safety net for the overnight window, and use Force charge for ad-hoc daytime charging. The plans are independent:

| You want | Do this | Result |
| --- | --- | --- |
| Guaranteed overnight charge (set and forget) | Keep `Charge with schedule` on | Charges every day inside the schedule window, regardless of price. |
| Daytime unconditional top‑up | Turn on `Force charge` with both force options off | Charges immediately, 24/7. The schedule stays enabled underneath and resumes when Force charge is turned off. |
| Daytime charge only when price is acceptable | Enable the Force acceptable-price option, then turn on `Force charge` | Waits only for `price <= acceptable_price`; the schedule window does not gate it. |
| Overnight, only when price is acceptable | Keep `Charge with schedule` and `Schedule acceptable price` on | Charges inside the schedule window only while `price <= acceptable_price`. |

### Native schedule reminders

In **Settings → Devices & services → SmartEVSE Dual Charger → Configure**:

- Keep **Schedule-disabled reminders** enabled for the existing persistent notice.
- Set **Notification action** to `notify.notify` for the default notifier or a specific `notify.mobile_app_*` action for a phone. Leave it empty to keep only the persistent warning (the backward-compatible default).
- Alternatively use `notify.send_message` and add one or more `notify.*` entity IDs under **Notification targets**. Legacy notify actions can use provider-specific targets; mobile-app-specific actions usually need none.
- **Advance reminder** defaults to 60 minutes; set it to 0 to send only the window-open notification. Reminders are sent only while **Charge with schedule** is off.

The next opening comes from the configured native schedule helper's `next_event`, not a hardcoded clock time. The helper already applies the Home Assistant time zone and daylight-saving rules. At window opening the integration reads `schedule.get_schedule` to recognize adjacent blocks as one continuous window. If Home Assistant restarts during an active window, it sends a missed window-open reminder once; it never sends an overdue advance reminder.

Successful delivery receipts survive restarts and integration updates. Delivery is claimed on disk before calling the notifier, so interruption during an uncertain send does not create a repeat on restart. A provider timeout is treated as uncertain delivery and is not retried for that window. Other notification failures retry after five minutes while the reminder remains applicable. Storage failure stops only phone reminders and leaves the existing storage intact. Notification work runs independently and cannot hold up charger control or meter pushes.

After enabling this feature, remove the old fixed-time **SmartEVSE schedule reminder** automation to avoid duplicate phone notifications. There is no change to charge policies or the **Charge with schedule** switch.

## Intelligent charging

Intelligent charging (0.0.9.0) is a standing plan, like `Charge with schedule`. Turn
on `Intelligent charging` once and it stays on through restarts and unplugs. For every
connected car it then charges in the cheapest 15-minute units that still bring the car
to its **minimum charge** by its **ready-by** time:

- It knows how many cars are connected (zero, one or two outlets). Only one charger
  charges at a time, because both share one feeder.
- It uses the consumer prices from Real Electricity Price (fees and taxes included).
- It reads each car's battery level from the configured vehicle battery sensor.
- Each detected car has its own ready-by time and minimum. A car the integration
  cannot identify uses the **Unidentified vehicle** ready-by time. Its battery level is
  unknown, so the plan charges the learned session energy (at least 30 kWh) by that
  time. There is no minimum setting for it.
- It predicts how much power SmartEVSE Smart mode will actually give the car in each
  hour of the night, from what it has learned about the household load.
- It learns locally, inside the integration: charging power, energy per percent,
  household load per hour, departure habits and a price forecast for hours that are
  not published yet. Nothing is sent to a cloud service, and no language model is used.

The integration still never writes charging currents. It only chooses which SmartEVSE
is in `Smart` and which is `Off`; SmartEVSE keeps adapting the current to the feeder.

Optional **top-up**: with `Intelligent top-up` on, a car that has reached its minimum
also charges, up to the car's own charge limit, in hours whose known price is at or
below `Intelligent top-up price`. Top-up never uses forecast prices and never moves
the minimum plan.

### What it needs

- **Price sensor**: the Real Electricity Price `current_price` sensor, configured as
  the integration's price sensor. Its `hourly_prices` lists for yesterday, today and
  tomorrow are found through the entity registry, so they must stay enabled.
  Tomorrow's prices are usually published between 14:00 and 15:00.
- **Vehicle battery sensors** in the integration options. For the Volvo integration,
  the battery-capacity and target-charge-level sensors next to the battery sensor are
  found automatically. The optional **battery capacity** options override the
  capacity (0 = automatic).
- **Vehicle connection-status sensors**, so a plugged-in car is identified and its own
  settings are used.
- **SmartEVSE in `Smart` mode**, with `current_main` (MaxMains), `current_min` and
  `current_max` set on the chargers. The integration reads them from `/settings`;
  16 A / 6 A / 16 A are assumed when they are missing.
- The companion card 0.3.0 or later for the Intelligent page, the plan list and the
  charger-tile lines. Older cards ignore the new attribute.

**Rolling back** to an integration version before 0.0.9.0: older versions ignore
`Intelligent charging`, so only `Charge with schedule` (if it is on) plans charging after
the rollback. Turn the schedule on first if the cars should still charge overnight.

### Entities

| Entity | Type | Default | Purpose |
| --- | --- | --- | --- |
| `Intelligent charging` | switch | off | Turns the plan on or off. Persists across restarts and unplugs. |
| `Intelligent top-up` | switch | off | Allows charging above the minimum in cheap hours. |
| `Intelligent top-up price` | number, currency/kWh | copy of `Acceptable price` | Top-up price limit. |
| `<Vehicle 1> ready by`, `<Vehicle 2> ready by` | time | 07:00 | When that car must have its minimum. |
| `Unidentified vehicle ready by` | time | 07:00 | Ready-by for a car that is not identified. |
| `<Vehicle 1> minimum charge`, `<Vehicle 2> minimum charge` | number, %, 10–100 | 80 % | Minimum charge level by the ready-by time. |
| `Intelligent charging status` | sensor (enum) | | Overall status, see below. |
| `Intelligent planned cost` | sensor (monetary) | | Planned cost of the connected cars' sessions: cost so far plus remaining planned cost. |
| `Intelligent planned saving` | sensor (monetary) | | Planned saving against charging at plug-in. Can be negative. |
| `Intelligent next charge` | sensor (timestamp) | | Start of the current or next planned charge (while a block runs, the time it started), or unknown. |

`<Vehicle 1>` and `<Vehicle 2>` are the configured vehicle names, for example
`time.smartevse_dual_charger_volvo_xc40_ready_by` and
`number.smartevse_dual_charger_volvo_ex30_minimum_charge`. Renaming a vehicle later
changes the friendly name, not the entity ID.

The money sensors take their currency from the price lists, have no state class
(they are plan values, not running totals) and are unknown until a plan with known
prices exists.

`Intelligent charging status` values:

| State | Meaning |
| --- | --- |
| `disabled` | Intelligent charging is off. |
| `overridden` | A Force charge plan is active; the plan resumes after it. |
| `no_ev` | No car is connected, or only one that charge policy keeps out of the plan. |
| `identifying` | A car was just plugged in and is not identified yet (up to 10 minutes). |
| `planned` | Waiting for the next planned block. |
| `charging` | A planned block is running. |
| `reached` | Every connected car has its minimum, or needs nothing more in this session (for example a session completed below its target). |
| `at_risk` | At least one car cannot reach its minimum by its ready-by time, or is past it. |
| `not_accepting` | A planned car is not drawing power. |
| `no_price_data` | No known prices: charging as late as safely possible before the deadline. |

The full plan is published as the `intelligent` attribute of the `Controller state`
sensor (status, per-charger plan blocks, per-car target, projected battery level,
cost and saving, hourly price slots, learning status, recent sessions and the entity
IDs of all Intelligent entities). The attribute is excluded from the recorder and only
changes at 15-minute boundaries or when an input changes, so the card does not re-render
every refresh.

### How the plan is built

The controller re-plans on every refresh. The planner is a pure function: the same
inputs always give the same plan.

1. **Time grid.** 15-minute units from now to the latest ready-by time. The unit that
   is already running is used only if at least 5 minutes remain (1 minute for the car
   that is already charging in it).
2. **Prices.** Each unit gets the price of its hour. Units are used in three tiers:
   - hours with a **known price**, cheapest first (negative prices are valid);
   - hours **without a known price** (not published yet, or missing), only for energy
     the known hours cannot deliver before the ready-by, and as late as safely possible.
     The learned price forecast is shown as an estimate but never chooses these hours;
   - while tomorrow's prices are still **expected** (15:00 local on the day before; Real
     Electricity Price usually has them by 14:00-15:00), known hours before that time
     come last: they are used only when waiting for the prices would miss a minimum.
   The plan is rebuilt every refresh, so it changes as soon as new prices arrive. After
   15:00 missing prices are late, and the plan no longer waits for them.
3. **Energy need.** From the battery level to the effective target, which is the
   minimum charge capped at the car's own charge limit. The need includes a charging
   taper above 93 % and margins: 3 % plus 3 minutes per expected start, and a
   conservative variant with the learned worst-case energy per percent, 10 % and
   10 minutes.
4. **Latest-start protection.** For each car the latest units before its deadline that
   cover its conservative need are reserved first. Looking for cheaper hours can
   therefore never make a car late.
5. **Which cars can make it.** If not every car can reach its minimum in time, the plan
   keeps the largest number of cars on time (the owner's policy, see below). The other
   cars get as much energy as they can before their ready-by without making an
   on-time car late. Such a car is `at_risk` when its plan ends below its minimum; if
   the plan still reaches it (only the safety margin is missing) it shows `tight`.
6. **Cheapest selection.** Units are taken cheapest first; on equal prices a unit next
   to an already chosen block wins, then the earlier one. A unit is only taken while it
   adds energy some on-time car can still use before its deadline. For one charger and
   per-car deadlines this greedy choice is exactly the cheapest plan.
7. **Safety buffer.** Extra units close to each deadline cover the gap between expected
   and conservative power, as long as they do not take time another car needs. A car
   whose buffer could not be completed shows `tight`.
8. **Blocks.** Consecutive units become blocks. A short block (under 30 minutes) is
   moved next to the car's other block when that costs at most €0.10 more, to avoid an
   extra start. Once a unit has started, it stays with its car until the unit ends,
   unless a car ranked before it (on time, or late but still able to reach its minimum)
   needs the rest of that unit; the hand-off then happens inside the unit. A start inside
   a unit gets at least 6 minutes there and happens up to a minute early, so the next
   cycle still finds enough of the unit left.
9. **Order.** When both cars need the same hours, the earlier ready-by charges first.
   `SmartEVSE N first` breaks ties; `SmartEVSE N only` leaves the other outlet out of the
   plan ("Not planned · charge policy").
10. **Top-up** (when enabled) adds known-price units at or below the top-up price, before
    the car's deadline, after the minimum plan is fixed.

**When not all cars can reach their minimum.** The owner's policy is to maximise the
number of cars that reach their minimum by their ready-by time. When the two cars each
fit alone but not together, the earlier ready-by stays on time, then the charge-policy
priority car (never the one with the smaller remaining need, which changes while a car
charges). A car that does not fit even alone is moved out. The cars that are moved out
get the most energy they can still get before their ready-by. The on-time cars still
reach their minimum on time, but they may be moved to dearer hours so that the late car
gets more energy, which changes their planned cost and saving. Among the late cars, a car
that can still reach its minimum without the safety margin is served first, then the
earlier ready-by.

**No price data.** When no known price covers the planning window (after falling back
to the last good price list the integration saved), every unit ranks equal. The plan
then covers the conservative need as late as possible before the deadline and stops at
the minimum, and `controller_error` is `intelligent_price_data_unavailable`. For the first
10 minutes after Home Assistant starts, missing prices only delay charging unless the time
left is barely enough.

### Smart-mode power prediction

SmartEVSE Smart mode gives the car what the feeder has left. The integration predicts
that per hour:

- Household load: each refresh it records the highest phase current of the mains meter
  minus the EV meter's current on that phase. Per local hour and weekday/weekend it keeps
  the hourly mean and 90th percentile of the last 14 such hours.
- Offered current: `min(MaxCurrent, MaxMains − household load)`. The car takes the smaller
  of that and its own learned current limit. If the offered current is below MinCurrent,
  SmartEVSE pauses ("No Power Available") and that hour gives 0 kW.
- Power: current × 3 phases × the learned kW per phase-amp (about 0.235 kW).
- Expected power uses the median household load; conservative power uses a high
  percentile with at least 1 A extra. An hour with fewer than 3 learned values uses
  3 A (expected) and 5 A (conservative), and until 7 days are learned conservative
  power is capped at 8.5 kW.
- What the house draws right now counts too: for the current and the next hour the
  household load of the last hour is a floor, and an hour without learned values uses
  it instead of the 3 A / 5 A defaults (at most 8 A / 9 A there, so an evening peak does
  not empty the night). This floor is re-read at the start of each 15-minute planning
  unit, in 0.5 A steps rounded up, so a spiky house does not change the plan in the
  middle of a unit.
- Expected power is calibrated against what the EV meter actually measured in recent
  5-minute charging windows (factor 0.5–1.10). The comparison uses the household load
  measured in the same window, so the factor describes the charger only; windows in
  which the car drew clearly less than it was offered are not used for it.
- A car that draws clearly less than it is offered (more than 2 A less, in at least two
  5-minute windows, for example with a cold battery) is planned at the median current
  it took in its last six such windows. This holds for the rest of that plug session,
  also while it waits for a cheaper slot or takes all of a lower offer during heat-pump
  hours. It ends when the car draws more than 1 A above that current, on unplug, or when
  Home Assistant restarts (then the evidence is gathered again within 10-15 minutes of
  charging). The other car keeps its own power in the plan.

A Smart-mode pause or a meter communication error is never treated as a full car or a
car that refuses charge (90-second guard after the pause clears). Hand-offs between
chargers are sequential in every mode: the outgoing charger is switched `Off` first, and
the incoming one is switched to `Smart` once the outgoing one is observed off and the EV
meter is below 1 kW, or 30 seconds after the Off write. This avoids a false "No Power
Available" start on the incoming charger.

### The learned model

The model lives in a private Home Assistant store,
`.storage/smartevse_dual_charger.<entry_id>.intelligent`. It is saved at most every
5 minutes after something new was learned, and on shutdown or reload. Every stored value
is validated on load; a bad value resets only itself.

| What | How it is learned | Until learned |
| --- | --- | --- |
| Household load per hour | Hourly mean and P90, weekday/weekend, last 14 per hour | 3 A expected, 5 A conservative |
| kW per phase-amp and each car's current limit | Clean 5-minute windows while one car charges | 0.235 kW, MaxCurrent − 0.8 A |
| Energy per % per car | One sample per clean plug session (≥ 15 points, ≤ 90 %), last 7, used after 3 | Capacity / 100 × 1.05 (capacity option, else Volvo sensor, else 75 kWh) |
| Session energy when the battery level is unknown | P80 of the last 10 clean sessions in the charging history, at least 30 kWh | 30 kWh |
| Departure habits | Unplug times 04:00–13:00 after an overnight session, weekday/weekend | No suggestion |
| Price forecast | Median of the last 14 known prices per local hour, weekday/weekend | The same hour of the most recent known day |

From the departure habits the card can suggest a ready-by time (shown as "Usually
leaves 07:30" with a "Use 07:15" action) once at least 8 departures are known.

If the store cannot be read (a corrupt, empty or unsupported file), the model runs on
defaults and never writes over the file. If a save fails (for example a full or read-only
disk), the learning stays unsaved in memory and is saved again later. In both cases
`controller_error` shows the warning `intelligent_model_unavailable`.

### Battery level, deadlines and sessions

- **Battery level sources**: a live reading; else the last reading of this plug session;
  with a reading older than 10 minutes, an estimate from the energy metered since it
  (also after the charger stopped, until a newer reading arrives); a reading that has
  not changed since well before the plug-in is "unverified"
  and gets one short probe instead of being trusted as "reached"; with no reading at all,
  the plan charges the learned session energy.
- Once a car has reached its minimum in a plug session, it is planned again only if its
  level falls 2 points below the target.
- **Deadline**: the next local occurrence of the ready-by time, built in local time, so
  daylight-saving changes keep the wall-clock time. A car plugged in less than 60 minutes
  before its ready-by time is planned for the next day.
- **At the ready-by time** the deadline moves to the next day. A car still below its
  minimum that was plugged in at least 60 minutes earlier is in **catch-up**: it charges
  in the earliest units until it reaches the minimum (`intelligent_catch_up`). A car that
  is charging towards a missed minimum keeps charging up to the ready-by time (it is not
  switched off for the last minute of the plan) and continues as catch-up. A ready-by
  time that passed more than 2 hours before the controller saw it (Home Assistant was
  down, or a version without Intelligent charging ran) starts no catch-up: the car is
  planned for the next ready-by time.
- **Identification during a session**: when a car is identified (or its mapping is
  corrected) while plugged in, its own ready-by time and minimum apply; a catch-up that
  is running continues, and a car whose own ready-by time has already passed since the
  plug-in is in catch-up as well.
- **Editing a ready-by time** less than 2 hours before the current deadline: an
  earlier time that is still ahead applies tonight; a later time, or one that has just
  passed, keeps tonight's deadline and applies from the next day. Editing a ready-by time or
  a minimum never ends a catch-up: it ends when the (new) minimum is reached, on unplug,
  or when Intelligent charging is switched off and on.
- **Charger off the API**: a charger that has not answered for `max(2 × refresh interval,
  60 s)` is left out of the plan, so the other car gets the time; its plug session,
  deadline and catch-up are kept and it is planned again as soon as it answers. While
  nothing charges and such a car may still need charge, the reason is
  `intelligent_planned_outlet_unavailable`.
- **Identifying**: for up to 10 minutes after a plug-in, an unidentified car waits
  (unless it must start now to make its deadline) and no cost or saving is shown for it.
- **Mapping checks**: a vehicle's "charging" report fixes the mapping only when it
  changed after the charger's current charging run began and at least 4 minutes after
  the other charger last charged (the vehicle cloud reports minutes late, so at a
  hand-off the car just switched off is still reported as charging). While Intelligent
  charging plans, a battery-level rise on the car mapped to the charger that is off also
  swaps a wrong mapping, but only when that car's own charger has not charged for
  30 minutes, the car on the charging charger stays flat, and the rise fits the metered
  energy.
- **Completion**: a session that SmartEVSE completes stays complete when the next block
  starts. If the battery level is still more than 1 point below the target 30 minutes
  later, the plan retries, at most twice.
- **Not accepting**: if a planned car in Smart mode does not start charging for 5 minutes, it is
  left out for 60 minutes and probed again at its next planned block; after 3 probes it is
  done for this plug session. A car whose battery level already shows its minimum (for
  example an unverified reading probed with one short charge) and that does not draw is
  full, not refusing: it counts as reached. If the car reports that its own charging timer
  is active, the controller shows `smartevse_N_vehicle_timer_blocks_charging` instead.

### Charge reasons

| Reason | Charging | Meaning |
| --- | --- | --- |
| `intelligent_charging` | yes | A planned block is running. |
| `intelligent_top_up` | yes | A top-up block is running for a car that has its minimum. |
| `intelligent_catch_up` | yes | A late or at-risk car is charging as early as possible. |
| `intelligent_no_price_data` | yes | Charging without price data, as late as safely possible. |
| `intelligent_waiting_for_planned_slot` | no | Waiting for the next planned block. |
| `intelligent_targets_reached` | no | Every connected car has its minimum. |
| `intelligent_vehicle_not_accepting` | no | Nothing left to plan, but a car below its minimum does not draw power. |
| `intelligent_charging_complete` | no | Nothing left to plan; a car's session completed below its minimum (or its level is unknown). |
| `smartevse_N_only_waiting_for_selected_ev` | no | `SmartEVSE N only` is selected and the only connected car is on the other charger. |
| `intelligent_waiting_for_connected_ev` | no | No car is connected. |
| `intelligent_identifying_vehicle` | no | A new car is being identified. |
| `intelligent_planned_outlet_unavailable` | no | The planned charger is unavailable or disconnected, or a car that may still need charge is on a charger that is off the API. |

Reasons are stable while nothing changes, so a reason change does not restart a cycle.

Warning-level `controller_error` values never block charging. Intelligent charging keeps
charging by deadline when they appear:

- `intelligent_price_data_unavailable`: no known prices; the no-price plan is used.
- `intelligent_planner_failed`: the planner failed; the no-price plan is used, never
  "charge now at any price".
- `intelligent_model_unavailable`: the learned model could not be loaded or saved;
  defaults are used.
- `smartevse_1_vehicle_timer_blocks_charging`, `smartevse_2_vehicle_timer_blocks_charging`:
  the planned car reports a charging status of `scheduled`, so its own timer may block
  charging.

Each part (model observation, model estimates, planning) runs in its own error guard;
the charger control loop never fails because of Intelligent charging.

### Saving: "vs charging at plug-in"

The saving compares the plan with plugging in and charging straight away:

- **Baseline**: each car charges continuously from its plug-in time at the expected
  power, one charger at a time in plug-in order, at the prices known at plug-in. The
  baseline energy is the energy delivered so far plus the remaining planned energy to
  the minimum. Top-up energy and cost are left out on both sides.
- **Per deadline window**: the baseline of the first window starts at the plug-in (at
  most 25 hours before the deadline). A window opened by a deadline roll (a second night,
  or catch-up after the ready-by time), or after another mode already charged in the
  session, starts its baseline when the window opens. The planned and the realised
  saving use the same start.
- **Planned saving** = baseline cost − (Intelligent charging cost so far in this window
  + remaining planned cost). For a car with nothing left to plan it is the window's
  realised (provisional) saving, the same figure History shows.
- The saving is unknown when part of it depends on forecast prices, unknown prices or
  a partial charging record. Savings smaller than €0.10 are published but the card hides
  them.
- **Realised saving**: when the deadline passes or the car is unplugged, the window is
  closed and the saving is recomputed from the charging-history record (measured energy
  and cost) against the same baseline. A window in which Force charge or another mode
  also charged is marked `mixed`, and the card shows "Force charge used" instead of a
  saving.

All amounts are estimates from power and prices, not a billing meter.

### Privacy

- Everything runs locally in Home Assistant. Prices, battery levels and charging data
  are not sent anywhere.
- Departure habits are kept only in the private store, as minutes of the day. The
  attribute and diagnostics only show the rounded suggestion, never the raw times.
- Diagnostics redact ready-by times, deadlines and suggestions, and round
  charging-history timestamps to the hour.
- The `intelligent` attribute is not written to the recorder. It is visible to every
  Home Assistant user, like the rest of the controller state, and contains the ready-by
  times and planned blocks.

### Limitations

- Prices are hourly (Real Electricity Price aggregates 15-minute prices); the plan
  itself uses 15-minute units.
- The model starts empty at installation. History from before the update is not used
  for household load, except the charging-history records for the session budget.
- Plans and savings are estimates. Household load can differ from the learned profile,
  for example when the heat pump runs harder than usual; the conservative power and
  the latest-start protection cover this, but the cost can come out higher.
- A car that was unplugged and plugged in again while Home Assistant was down is not
  detected as a new session.
- A saving needs known prices from the start of its baseline (the plug-in, or the
  start of the deadline window).
- With 15-minute price lists the realised-saving snapshot keeps the hourly mean; an hour
  with an unpriced quarter counts as unpriced.
- There is no notification for `at_risk`; use the `Intelligent charging status` sensor
  in an automation.
- No coordination with the heat pump or other large loads.
- The charging-plan chart on the card is planned for a later card release; the payload
  already contains the hourly slots.

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
- `Schedule acceptable price`
- `Intelligent charging`
- `Intelligent top-up`

### Numbers

All numeric inputs use Home Assistant number entities with box input.

- `Acceptable price`
- `Force charge duration`
- `Duty cycle`
- `Controller refresh interval`
- `Mains current push interval`
- `EV meter push interval`
- `Intelligent top-up price`
- `<Vehicle 1> minimum charge`, `<Vehicle 2> minimum charge`

### Selects

- `Charge policy`

### Times

- `<Vehicle 1> ready by`, `<Vehicle 2> ready by`, `Unidentified vehicle ready by`

`<Vehicle 1>` and `<Vehicle 2>` are the configured vehicle names.

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
- `SmartEVSE 1 session cost`
- `SmartEVSE 2 session cost`
- `Intelligent charging status`
- `Intelligent planned cost`
- `Intelligent planned saving`
- `Intelligent next charge`

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
- `charging_costs` (charging history) and `intelligent` (the Intelligent charging plan); both are excluded from the recorder

### Service Actions

- `smartevse_dual_charger.refresh`
- `smartevse_dual_charger.reset_sessions`

Behavior:

- `refresh`: runs one controller cycle immediately
- `reset_sessions`: clears remembered per-EV completion state, the per-car Intelligent charging state (deadline, catch-up, not-accepting) and active SmartEVSE selection, then starts a fresh cycle; the learned model and charging history are kept

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

Intelligent charging adds warning-level values. They never block charging; the plan keeps charging by deadline:

- `intelligent_price_data_unavailable`
- `intelligent_planner_failed`
- `intelligent_model_unavailable`
- `smartevse_1_vehicle_timer_blocks_charging`
- `smartevse_2_vehicle_timer_blocks_charging`

See [Charge reasons](#charge-reasons) for what each one means.

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
- [`custom_components/smartevse_dual_charger/charge_planner.py`](custom_components/smartevse_dual_charger/charge_planner.py): Intelligent charging planner (pure Python, no Home Assistant imports)
- [`custom_components/smartevse_dual_charger/charge_model.py`](custom_components/smartevse_dual_charger/charge_model.py): Intelligent charging learned model (pure Python)
- [`custom_components/smartevse_dual_charger/intelligent.py`](custom_components/smartevse_dual_charger/intelligent.py): Intelligent charging adapter: source entities, model storage, plan inputs, published plan
- [`custom_components/smartevse_dual_charger/coordinator.py`](custom_components/smartevse_dual_charger/coordinator.py): refresh scheduling, push loops, immediate price/schedule refresh triggers
- [`custom_components/smartevse_dual_charger/number.py`](custom_components/smartevse_dual_charger/number.py): runtime number entities, including the Intelligent minimum charge and top-up price
- [`custom_components/smartevse_dual_charger/select.py`](custom_components/smartevse_dual_charger/select.py): runtime charge policy select
- [`custom_components/smartevse_dual_charger/sensor.py`](custom_components/smartevse_dual_charger/sensor.py): controller, SmartEVSE detail, session cost and Intelligent charging sensors
- [`custom_components/smartevse_dual_charger/switch.py`](custom_components/smartevse_dual_charger/switch.py): force, schedule and Intelligent charging switches
- [`custom_components/smartevse_dual_charger/time.py`](custom_components/smartevse_dual_charger/time.py): ready-by time entities
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

## Integration icon

Home Assistant 2026.3 and later displays the bundled integration icon from
`custom_components/smartevse_dual_charger/brand/`. The editable artwork and export
instructions are in [branding/](branding/README.md). Earlier Home Assistant
versions retain their usual fallback icon.

## Charging costs and history

Each outlet now has a **session cost** sensor. The companion card displays the
current/last session estimate beneath the vehicle battery level. Its settings
contain the latest 100 sessions per charger, including date, vehicle, estimated
kWh, cost, weighted average price and any missing-data warnings.

Accounting observes the configured EV power meter and price sensor (use **Real
Electricity Price — current price**, which already includes its configured
fees/taxes). It integrates measured W/kW at the tariff observed at each poll;
negative prices are supported. This is a power-based estimate, not a billing
meter. The shared meter is assigned only when exactly one available charger
reports Charging. Never configure a whole-house power sensor here.

A session starts when charging is observed and remains the same through pauses,
price gates and duty-cycle handovers until unplugging. Session completion remains
visible while plugged in. History and checkpoints are stored by the integration
in a separate HA storage file, shared by every user and retained through restarts
and upgrades. Resetting charging-control sessions does not delete the ledger.
Tracking starts at installation; earlier charges are not reconstructed.

Unavailable power/prices, ambiguous shared-meter use and time while Home Assistant
was stopped are flagged as partial. Unobserved intervals are **not** filled with
the last power value or today's tariff. The estimated cost is the known subtotal;
expand a history entry to see its gaps. Keep the HA storage directory in backups.

Developer checks: Python 3.14 with `requirements-test.txt`, then
`python -m unittest discover -s test -v`. Tests use synthetic samples and mocked
storage only; they never issue charger commands.

## Charging history reliability (0.0.8.10)

History writes are atomic and propagate disk/serialization failures to the card.
Accounting failures remain separate from charger control. Corrupt nested history
or checkpoints are rejected without overwriting the stored ledger. A charge
first observed after a restart or telemetry gap is explicitly a partial estimate.
