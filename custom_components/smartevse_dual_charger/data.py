"""Runtime data for the SmartEVSE Dual Charger integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .controller import SmartEVSEDualChargerController
    from .coordinator import SmartEVSEDualChargerCoordinator


@dataclass(slots=True)
class SmartEVSEDualChargerData:
    """Runtime data stored on the config entry."""

    controller: SmartEVSEDualChargerController
    coordinator: SmartEVSEDualChargerCoordinator
    integration: Integration


type SmartEVSEDualChargerConfigEntry = ConfigEntry[SmartEVSEDualChargerData]

