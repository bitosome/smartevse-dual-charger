# SmartEVSE Dual Charger — Home Assistant Configuration

## Custom Integration

This repository now includes a HACS-compatible custom integration at [custom_components/smartevse_dual_charger](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger).

The integration replaces the helper-heavy YAML automation with a controller that:

- owns the dual-EVSE balancing loop
- fixes the timer unplug bug
- stops using `ChargeCurrent` as if it were actual draw
- can push `/currents` and `/ev_meter` to both SmartEVSE devices
- can drive WLED directly without `rest_command`
- exposes controller switches, numbers, select entities, sensors, and buttons through the HA UI

Recommended architecture for this integration:

- leave the SmartEVSE devices on MQTT for native per-device entities
- configure this controller through the UI
- use `Normal` as the controller active mode so Home Assistant is the single balancing authority
- remove the SmartEVSE-specific `rest:` and `rest_command:` blocks from [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml) once the integration is in use

### HACS layout

The repository now contains:

- [custom_components/smartevse_dual_charger](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger)
- [hacs.json](/Users/arku02/Repositories/smartevse-dual-charger/hacs.json)

The repository now also includes the required HACS brand asset at [brands/icon.png](/Users/arku02/Repositories/smartevse-dual-charger/brands/icon.png).

### Integration Entities

The controller creates entities such as:

- `switch.smartevse_dual_charger_force_charge`
- `switch.smartevse_dual_charger_force_price`
- `switch.smartevse_dual_charger_force_timer`
- `switch.smartevse_dual_charger_charge_with_schedule`
- `number.smartevse_dual_charger_balance_percent`
- `number.smartevse_dual_charger_acceptable_price`
- `number.smartevse_dual_charger_force_charge_duration_minutes`
- `select.smartevse_dual_charger_low_budget_policy`
- `sensor.smartevse_dual_charger_controller_state`
- `sensor.smartevse_dual_charger_available_current`
- `sensor.smartevse_dual_charger_evse_1_target_current`
- `sensor.smartevse_dual_charger_evse_2_target_current`

### Card Example

Use [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml) as the updated Lovelace example for the custom integration. It keeps the native SmartEVSE MQTT status cards and swaps the old helper entities for the new controller entities.

## Legacy YAML

The original YAML automation and helper-based setup are still present in this repository as legacy reference:

- [automation.yaml](/Users/arku02/Repositories/smartevse-dual-charger/automation.yaml)
- [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml)
- [card.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card.yaml)

Two [SmartEVSE](https://smartevse.nl/) devices in a shared enclosure, controlled by a single Home Assistant automation with dynamic current balancing and WLED status LEDs.

## Hardware

| Device | Hostname | IP | Role |
|--------|----------|-----|------|
| SmartEVSE-1 | smartevse-6374 | `192.168.0.234` | EV charger (left cable) |
| SmartEVSE-2 | smartevse-8717 | `192.168.0.44` | EV charger (right cable) |
| Shelly Pro 3EM 1 | — | — | Mains meter (grid import) |
| Shelly Pro 3EM 2 | — | — | EV meter (charger circuit) |
| WLED (ESP32-S3) | — | `192.168.0.81` | 105 LEDs, status indicator |
| Home Assistant | — | `192.168.0.13` | Automation controller |

**Electrical:** Both SmartEVSE devices share a single 16A power line with individual contactors and separate EV cables.

## Files

| File | Purpose |
|------|---------|
| `automation.yaml` | Single automation controlling both chargers, current balancing, WLED, and timer |
| `configuration.yaml` | REST endpoints (SmartEVSE + WLED), plus other HA config (not all SmartEVSE-specific) |
| `card.yaml` | Lovelace dashboard card (paste into a manual card or raw YAML editor) |

> **Note:** `configuration.yaml` contains the full HA config including unrelated sensors (NIBE heat pump, Aqara temperature sensors, etc.). The SmartEVSE-specific sections are `rest_command:` and `rest:`.

## MQTT Entities

Both SmartEVSE devices communicate via MQTT. Key entities:

### SmartEVSE-1 (`smartevse_1_*`)

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.smartevse_1_state` | Sensor | Charging state (Charging, Ready to Charge, Connected to EV, Charging Stopped, Stop Charging) |
| `sensor.smartevse_1_evplugstate` | Sensor | Plug state (Connected / Disconnected) |
| `select.smartevse_1_mode` | Select | Mode control (Smart / Off) |
| `sensor.smartevse_1_chargecurrent` | Sensor | Actual charging current (A) |
| `sensor.smartevse_1_maxcurrent` | Sensor | Max current (read-only, A) |
| `number.smartevse_1_chargecurrentoverride` | Number | Writable charge current override (0–16A, step 1) |
| `sensor.smartevse_1_evchargepower` | Sensor | EV charge power (W) |
| `sensor.smartevse_1_error` | Sensor | Error state |

### SmartEVSE-2 (`smartevse_2_*`)

Same entity pattern with `smartevse_2_` prefix.

### Shelly Pro 3EM Sensors

| Entity | Description |
|--------|-------------|
| `sensor.shelly_pro_3em_1_phase_[a\|b\|c]_current` | Mains per-phase current (A) |
| `sensor.shelly_pro_3em_1_total_active_power` | Mains total power (W) |
| `sensor.shelly_pro_3em_2_phase_[a\|b\|c]_current` | EV meter per-phase current (A) |
| `sensor.shelly_pro_3em_2_total_active_power` | EV meter total power (W) |
| `sensor.shelly_pro_3em_2_total_active_energy` | EV meter total energy (kWh) |
| `sensor.shelly_pro_3em_2_total_active_returned_energy` | EV meter returned energy (kWh) |

## REST Endpoints

HA pushes meter data to each SmartEVSE device via REST:

### Mains Currents → SmartEVSE
```
POST http://<smartevse_ip>/currents?L1=<val>&L2=<val>&L3=<val>
```
- Values are in deci-amps (current × 10, rounded to int)
- Source: Shelly Pro 3EM 1 (mains meter)
- Scan interval: 10 seconds
- Sent to both `192.168.0.234` and `192.168.0.44`

### EV Meter → SmartEVSE
```
POST http://<smartevse_ip>/ev_meter?L1=<val>&L2=<val>&L3=<val>&import_active_energy=<wh>&export_active_energy=<wh>&import_active_power=<w>
```
- Currents in deci-amps, energy in Wh, power in W
- Source: Shelly Pro 3EM 2 (EV meter)
- Scan interval: 20 seconds
- Sent to both SmartEVSE devices

### WLED API
```
POST http://192.168.0.81/json/state
```
- Controlled via `rest_command.wled_api` with templated JSON payload
- No HA WLED integration needed (firmware 16.x incompatible with HA integration)

## Automation Logic

The automation (`SmartEVSE dual charger control`) runs in **parallel mode** and is triggered by EVSE state changes, charge control toggles, schedule, electricity price changes, and a 10-second periodic tick for current balancing.

### 1. Mutual Exclusion of Force Charge Modes

Three force charge modes are mutually exclusive — enabling one disables the others:
- **Force charge** (`input_boolean.force_charge_state`)
- **Force charge at acceptable price** (`input_boolean.force_charge_with_acceptable_electricity_price_state`)
- **Force charge with timer** (`input_boolean.force_charge_with_timer_state`)

On HA restart, the timer mode is always turned off (timer state can't survive a restart).

### 2. Mode Control (per EVSE, independent)

Each EVSE's mode is controlled independently based on shared charge conditions:

| Condition | Mode Set |
|-----------|----------|
| EV not plugged in | Off |
| EV plugged in + any charge condition active | Smart |
| EV plugged in + no charge condition active | Off |

**Charge conditions** (any one activates Smart mode):
- Force charge toggle ON
- Schedule gate ON + schedule window active
- Price-based charge ON + current price ≤ acceptable price
- Timer charge ON

Mode changes include a guard to skip the MQTT write if the device is already in the target mode.

### 3. Dynamic Current Balancing

HA acts as a central load balancer to prevent a race condition where both SmartEVSE devices read the same mains current and both claim the same headroom.

**Algorithm (runs every 10 seconds):**
1. Read peak mains current across all 3 phases (Shelly Pro 3EM 1)
2. Subtract both EVSEs' charge currents to get house load
3. Calculate available headroom: `16A - house_load`
4. Split by configurable ratio (`input_number.charger_current_balance`, 0–100%)
5. Write to `number.smartevse_X_chargecurrentoverride`

**Rules:**
- Minimum 6A per EVSE (SmartEVSE hardware minimum)
- Maximum 16A per EVSE
- If one EVSE is done charging (current < 0.5A or state = Charging Stopped/Stop Charging), its share goes to the active EVSE
- If both inactive, both get 6A (minimum)
- If only one connected, it gets the full available headroom

**Contactor protection:** A 2A hysteresis deadband prevents override updates for small fluctuations. The override is only written when the calculated share differs from the current setting by ≥ 2A. This prevents SmartEVSE's stop/restart cycle on every override change.

### 4. Schedule Notification

When a scheduled charge window starts (`schedule.charge_schedule` turns on) but the schedule gate (`input_boolean.charging_schedule_state`) is disabled, a `notify.notify` notification is sent reminding the user to enable the schedule.

### 5. Force Charge Reset

All force charge toggles are turned off when **both** chargers are unplugged. This prevents stale toggles when cars are removed.

### 6. Timer Management

When the timer is activated:
1. Waits for either: timer expiry, manual toggle off, or both EVSEs unplugged
2. Always turns off the timer toggle when the wait ends (timeout or early exit)

On HA restart, the timer is always reset to off.

### 7. WLED Status LEDs

105 LEDs split into two independent segments:
- **Segment 0** (LEDs 0–52): SmartEVSE-1 (left side)
- **Segment 1** (LEDs 53–104): SmartEVSE-2 (right side)

Each segment reflects its EVSE's state independently:

| EVSE State | Color | Effect (fx) |
|------------|-------|-------------|
| Unplugged | Off | — |
| Charging (≥ 1A) | Green `[0,255,0]` | Chase (28) |
| Ready to Charge | Blue `[0,100,255]` | Breathe (2) |
| Connected to EV / Charging < 1A | Amber `[255,160,0]` | Solid (0) |
| Stop Charging | Orange `[255,80,0]` | Wipe (3) |
| Charging Stopped | Blue `[0,100,255]` | Breathe (2) |
| Unknown/other | Dim white `[100,100,100]` | Solid (0) |

**Global override:** If either EVSE reports an error, the entire strip pulses red (Breathe effect).

WLED updates only fire on EVSE state/plug changes and HA start — **not** on the 10-second balance tick, to avoid unnecessary HTTP traffic.

### WLED Presets (saved on device)

Presets are saved on the WLED device for manual testing via the WLED UI:

| ID | Name | Description |
|----|------|-------------|
| 1 | Off | All LEDs off |
| 2 | SmartEVSE-1 Charging | Left green chase, right off |
| 3 | SmartEVSE-1 Ready | Left blue breathe, right off |
| 4 | SmartEVSE-1 Connected | Left amber solid, right off |
| 5 | SmartEVSE-1 Stopped | Left blue breathe, right off |
| 6 | SmartEVSE-2 Charging | Left off, right green chase |
| 7 | SmartEVSE-2 Ready | Left off, right blue breathe |
| 8 | SmartEVSE-2 Connected | Left off, right amber solid |
| 9 | SmartEVSE-2 Stopped | Left off, right blue breathe |
| 11 | Error | Full strip red pulsing |

The automation does **not** use presets — it builds the JSON payload dynamically so both segments can show different states simultaneously (e.g., left charging + right connected).

## Dashboard Card

The Lovelace card (`card.yaml`) uses [Mushroom](https://github.com/piitaya/lovelace-mushroom) cards and provides:

- **Electricity price chip** — green/red based on acceptable price threshold
- **SmartEVSE-1 status** — state, plug, mode, charge current, max current, override
- **SmartEVSE-2 status** — same layout
- **Schedule info** — armed/active/disabled with next event time
- **Force charge** — tap to toggle, shows waiting/active state
- **Price-based charge** — shows current price vs threshold
- **Timer charge** — shows configured duration
- **Settings** — schedule toggle, charge schedule, current balance slider, acceptable price, timer duration

EVSE status card icon colors match the WLED LED colors (green=charging, blue=ready/stopped, amber=connected, orange=stopping, grey=unplugged).

## HA Helpers (create via UI)

These helpers must be created manually in HA (Settings → Devices & Services → Helpers):

| Helper | Type | Settings |
|--------|------|----------|
| `input_boolean.force_charge_state` | Toggle | Force charge |
| `input_boolean.force_charge_with_acceptable_electricity_price_state` | Toggle | Price-based charge |
| `input_boolean.force_charge_with_timer_state` | Toggle | Timer charge |
| `input_boolean.charging_schedule_state` | Toggle | Schedule gate |
| `input_number.charger_current_balance` | Number | Min: 0, Max: 100, Step: 1, Unit: %, Mode: slider, Icon: mdi:scale-balance |
| `input_datetime.force_charge_time` | Date/Time | Time only (HH:MM:SS) |
| `schedule.charge_schedule` | Schedule | Weekly charge schedule |

## External Dependencies

| Component | Required | Purpose |
|-----------|----------|---------|
| [Mushroom cards](https://github.com/piitaya/lovelace-mushroom) | Yes (HACS) | Dashboard UI cards |
| MQTT broker | Yes | SmartEVSE communication |
| [Real Electricity Price](https://github.com/jomwells/ha-real-electricity-price) | Yes | Price-based charging (sensor + number entities) |

## Corner Cases Handled

- **Both EVSEs claim same headroom**: Prevented by HA acting as central load balancer
- **Contactor cycling on current changes**: 2A hysteresis deadband on override updates
- **HA restart during timer charge**: Timer is always reset to off on startup
- **One EV finishes before the other**: Idle EVSE's share is reallocated to the active one
- **Both EVs unplugged**: All force charge toggles reset, WLED turns off, modes set to Off
- **WLED firmware incompatibility**: Bypassed entirely via direct HTTP API (no HA integration)
- **Sensors unavailable**: `float(0)` fallbacks throughout; mode guards prevent redundant MQTT writes
- **Force charge mode conflicts**: Mutually exclusive — enabling one disables the others
- **WLED unreachable**: `rest_command` silently fails without blocking the automation
