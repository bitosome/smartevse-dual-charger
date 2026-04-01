# SmartEVSE Dual Charger

Home Assistant controller for two standalone SmartEVSE chargers sharing one feeder.

## Status

This repository now has two implementations:

- Current implementation: the HACS-compatible custom integration in [custom_components/smartevse_dual_charger](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger)
- Legacy reference: the YAML automation and helper-based setup in [automation.yaml](/Users/arku02/Repositories/smartevse-dual-charger/automation.yaml), [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml), and [card.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card.yaml)

For any future implementation work, treat the custom integration as the source of truth. The YAML files remain in the repo so the next agent can compare behavior, migrate missing pieces, or verify parity.

Current maturity:

- Integration scaffold is implemented and packaged for HACS
- Core controller loop is implemented
- Lovelace example for the integration is provided
- Static validation was run
- Live Home Assistant validation has not been completed yet
- Automated tests have not been added yet

## Handoff Summary

The next LLM agent should start here:

- Runtime entry points: [custom_components/smartevse_dual_charger/__init__.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/__init__.py), [custom_components/smartevse_dual_charger/config_flow.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/config_flow.py), [custom_components/smartevse_dual_charger/controller.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/controller.py), [custom_components/smartevse_dual_charger/coordinator.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/coordinator.py)
- UI entities: [custom_components/smartevse_dual_charger/switch.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/switch.py), [custom_components/smartevse_dual_charger/number.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/number.py), [custom_components/smartevse_dual_charger/select.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/select.py), [custom_components/smartevse_dual_charger/sensor.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/sensor.py), [custom_components/smartevse_dual_charger/button.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/button.py)
- Packaging and metadata: [custom_components/smartevse_dual_charger/manifest.json](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/manifest.json), [hacs.json](/Users/arku02/Repositories/smartevse-dual-charger/hacs.json), [custom_components/smartevse_dual_charger/translations/en.json](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/translations/en.json)
- Example dashboard: [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml)
- Reference firmware and prior art: [references](/Users/arku02/Repositories/smartevse-dual-charger/references)

The main design assumption in the integration is:

- Keep the native SmartEVSE MQTT entities as the per-device interface
- Let this integration be the single dual-charger controller
- Prefer SmartEVSE mode `Normal` when HA owns balancing
- Avoid running the legacy automation and the integration at the same time

If a future agent wants to revisit `PWR SHARE`, that is a separate architecture. The current integration targets the "two standalone EVSEs controlled by HA" approach.

## Repository Layout

| Path | Role |
|------|------|
| [custom_components/smartevse_dual_charger](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger) | Current Home Assistant integration |
| [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml) | Updated Lovelace example for the integration |
| [hacs.json](/Users/arku02/Repositories/smartevse-dual-charger/hacs.json) | HACS metadata |
| [brands/icon.png](/Users/arku02/Repositories/smartevse-dual-charger/brands/icon.png) | Brand asset placeholder |
| [automation.yaml](/Users/arku02/Repositories/smartevse-dual-charger/automation.yaml) | Legacy automation reference |
| [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml) | Legacy REST and helper setup reference |
| [card.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card.yaml) | Legacy Lovelace card reference |
| [references](/Users/arku02/Repositories/smartevse-dual-charger/references) | SmartEVSE firmware/docs and HACS reference projects |

## Hardware and Existing HA Dependencies

Physical setup:

- Two SmartEVSE devices share one 16A feed
- Each EVSE still has its own contactor and EV cable
- Shelly Pro 3EM #1 measures mains current
- Shelly Pro 3EM #2 measures the EV charger circuit
- Optional WLED strip mirrors status

The custom integration does not replace these external dependencies:

- Native MQTT-discovered SmartEVSE entities
- Mains current sensors
- EV meter sensors
- Optional electricity price sensor
- Optional schedule entity
- Optional WLED device reachable over HTTP

It does replace the SmartEVSE-specific `rest:` endpoints and WLED `rest_command` previously defined in [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml).

## What The Integration Implements

The controller in [custom_components/smartevse_dual_charger/controller.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/controller.py) currently covers these behaviors from the old automation:

- Force charge
- Price-gated charging
- Timer-based charging
- Schedule-gated charging
- Mutual exclusion between force-charge modes
- Auto-reset of timer mode when it expires
- Auto-stop of timer mode only when both EVs are unplugged
- EVSE mode writes through existing MQTT `select` entities
- EVSE current override writes through existing MQTT `number` entities
- Periodic `/currents` POST to both SmartEVSE devices
- Periodic `/ev_meter` POST to both SmartEVSE devices
- Direct WLED JSON updates
- Persistent schedule-disabled notification when a schedule window starts
- Persistent internal state using HA storage

Behavioral improvements over the YAML automation:

- Timer unplug bug is fixed: timer mode is no longer cancelled just because one EV unplugged
- Current allocation no longer uses `ChargeCurrent` as if it were measured draw
- Session tracking distinguishes "finished charging but still plugged in" from "still needs current"
- Low-budget behavior is explicit when available current is less than `2 * min_current`

## Current Balancing Logic

The controller uses:

- Mains peak current from the three configured mains entities
- EV meter peak current from the three configured EV meter entities
- `house_load = mains_peak - ev_meter_peak`
- `available_current = total_current_limit - house_load`

Allocation rules:

- If charging is not allowed, both EVSE targets go to `0`
- If only one EVSE is a candidate, it can take the full available current
- If both EVSEs are candidates and there is enough current for both, the remainder above `min_current` is split by `balance_percent`
- If available current is below `2 * min_current`, the configured low-budget policy decides whether one EVSE wins or both pause

Supported low-budget policies:

- `alternate`
- `evse_1_priority`
- `evse_2_priority`
- `pause_all`

The controller treats SmartEVSE state as follows:

- `Charging` means active
- `Ready to Charge` means pending
- `Connected to EV` means pending before the session has charged, and complete after the session has charged
- `Charging Stopped` and `Stop Charging` mean complete once that session already charged

That state tracking is the fix for the earlier case where one EV finished, remained plugged in, and incorrectly kept half of the current budget.

## Configuration Flow

The integration is configured from the UI. The initial config flow currently asks for:

- Name
- EVSE 1 base URL
- EVSE 2 base URL
- Optional WLED URL
- EVSE 1 state, plug, mode, override, and optional error entities
- EVSE 2 state, plug, mode, override, and optional error entities
- Mains L1, L2, L3 current entities
- EV meter L1, L2, L3 current entities
- EV meter import active power entity
- Optional EV meter import and export energy entities
- Optional price sensor entity
- Optional schedule entity

Options flow currently exposes:

- Active SmartEVSE mode to use while charging is allowed: `Normal` or `Smart`
- Total current limit
- Minimum current
- Controller update interval
- Override deadband
- Default low-budget policy
- Whether to push `/currents`
- `/currents` push interval
- Whether to push `/ev_meter`
- `/ev_meter` push interval
- Whether to push WLED
- Whether to create schedule-disabled notifications

Recommended default for HA-owned balancing:

- `active_mode = Normal`

Using `Smart` here may reintroduce the "HA and SmartEVSE both regulate current" problem that existed in the YAML design.

## Entities Created By The Integration

The controller creates these entity types:

- Switches: force charge, force price, force timer, charge with schedule
- Numbers: balance percent, acceptable price, force charge duration minutes
- Select: low-budget policy
- Sensors: controller state, charge reason, available current, house load, EVSE 1 target current, EVSE 2 target current, timer remaining
- Buttons: refresh, reset session tracking

It also exposes services from [custom_components/smartevse_dual_charger/services.yaml](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/services.yaml):

- `smartevse_dual_charger.refresh`
- `smartevse_dual_charger.reset_sessions`

The integration provides diagnostics in [custom_components/smartevse_dual_charger/diagnostics.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/diagnostics.py), with EVSE and WLED URLs redacted.

## Lovelace

Use [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml) as the current card example.

Important notes:

- It keeps the native SmartEVSE MQTT status cards
- It swaps legacy helper entities for integration-owned entities
- The example assumes the config entry title remains `SmartEVSE Dual Charger`
- If the config entry title changes, the generated entity IDs will change and the card must be updated accordingly

The old [card.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card.yaml) is retained only as a reference for the prior helper-based dashboard.

## HACS Packaging

The repository is structured to be installable through HACS:

- Integration code in [custom_components/smartevse_dual_charger](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger)
- Metadata in [hacs.json](/Users/arku02/Repositories/smartevse-dual-charger/hacs.json)
- Manifest in [custom_components/smartevse_dual_charger/manifest.json](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/manifest.json)
- Brand asset in [brands/icon.png](/Users/arku02/Repositories/smartevse-dual-charger/brands/icon.png)

The icon is currently a placeholder and should be replaced before publishing widely.

## Migration From The Legacy YAML

Recommended migration order:

1. Install the integration from this repo through HACS or by copying [custom_components/smartevse_dual_charger](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger) into `custom_components`.
2. Keep the native MQTT SmartEVSE entities enabled.
3. Create the config entry and map it to the existing SmartEVSE, Shelly, price, and schedule entities.
4. Confirm the new integration entities appear.
5. Update the dashboard using [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml).
6. Disable the legacy automation in [automation.yaml](/Users/arku02/Repositories/smartevse-dual-charger/automation.yaml).
7. Remove or disable the SmartEVSE-specific `rest:` endpoints and the WLED `rest_command` from [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml).
8. Verify that only one control path remains active.

Do not leave the old automation and the new integration active together. That would create duplicate mode writes, duplicate override writes, duplicate SmartEVSE API pushes, and duplicate WLED updates.

## What Has Been Verified

Static checks already run:

- `python3 -m compileall custom_components/smartevse_dual_charger`
- JSON parsing for [custom_components/smartevse_dual_charger/manifest.json](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/manifest.json)
- JSON parsing for [custom_components/smartevse_dual_charger/translations/en.json](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/translations/en.json)
- JSON parsing for [hacs.json](/Users/arku02/Repositories/smartevse-dual-charger/hacs.json)
- YAML parsing for [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml)

What has not been verified yet:

- A full Home Assistant runtime test
- Config flow behavior in the HA UI
- Entity registry names in a real installation
- SmartEVSE HTTP push behavior against the actual devices
- WLED behavior against the actual strip
- Regression coverage with automated tests

## Known Gaps And Next Work

Known parity gaps already identified:

- Legacy behavior "turn off force charge and force price when both EVs unplug" has not been ported yet. The current integration only auto-clears the timer mode on both-unplugged.
- Legacy behavior "always reset timer mode on HA restart" has not been ported yet. The current integration persists timer state in storage.
- Legacy automation used `notify.notify` for the schedule reminder. The current integration uses a persistent notification instead.

The next agent should prioritize:

1. Run the integration in a live HA environment and validate config flow, entity creation, service calls, and the update cycle.
2. Decide whether to port the remaining parity gaps exactly, especially force-mode reset on both-unplugged and timer reset on restart.
3. Validate that `Normal` mode plus override writes gives the intended SmartEVSE behavior on real hardware.
4. Confirm whether `/currents` and `/ev_meter` should stay enabled by default in HA-owned balancing mode, or become optional/off by default.
5. Add automated tests for allocation, timer expiry, timer unplug handling, low-budget winner selection, and WLED payload generation.
6. Replace the placeholder brand icon.
7. Decide whether the repo should keep the legacy YAML files long-term or move them into a dedicated `legacy/` directory.

Implementation hotspots for follow-up work:

- Balancing and policy logic: [custom_components/smartevse_dual_charger/controller.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/controller.py)
- Config and options UX: [custom_components/smartevse_dual_charger/config_flow.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/config_flow.py)
- Entity surface and naming: [custom_components/smartevse_dual_charger/sensor.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/sensor.py), [custom_components/smartevse_dual_charger/switch.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/switch.py), [custom_components/smartevse_dual_charger/number.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/number.py), [custom_components/smartevse_dual_charger/select.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/select.py), [custom_components/smartevse_dual_charger/button.py](/Users/arku02/Repositories/smartevse-dual-charger/custom_components/smartevse_dual_charger/button.py)
- Dashboard parity: [card_integration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card_integration.yaml)

## Legacy Reference

The old YAML implementation is still useful for behavior comparison:

- [automation.yaml](/Users/arku02/Repositories/smartevse-dual-charger/automation.yaml) shows the original trigger and helper-driven flow
- [configuration.yaml](/Users/arku02/Repositories/smartevse-dual-charger/configuration.yaml) shows the previous REST wiring to SmartEVSE and WLED
- [card.yaml](/Users/arku02/Repositories/smartevse-dual-charger/card.yaml) shows the original helper-based dashboard

The [references](/Users/arku02/Repositories/smartevse-dual-charger/references) directory now contains:

- SmartEVSE source and docs for firmware behavior checks
- A backend HACS integration reference
- A frontend card reference

Those references are intended for future implementation work and code review, not as runtime dependencies.
