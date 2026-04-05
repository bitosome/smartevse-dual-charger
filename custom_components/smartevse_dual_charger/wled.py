"""WLED helpers for SmartEVSE Dual Charger."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError, FormData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_WLED_LED_COUNT, DEFAULT_WLED_LED_OFFSET

WLED_LED_MAP_ID = 0
WLED_PRESET_NAME_PREFIX = "SmartEVSE "
WLED_PRESET_ID_START = 101
WLED_PRESET_ID_LIMIT = 250

COLOR_CHARGING = [0, 255, 0]
COLOR_IDLE = [0, 100, 255]
COLOR_ERROR = [255, 0, 0]

CHARGING_FX = 41
CHARGING_SX = 80
CHARGING_IX = 100
CHARGING_PAL = 2
CHARGING_C1 = 128
CHARGING_C2 = 128
CHARGING_C3 = 16

IDLE_FX = 2
IDLE_SX = 45
IDLE_IX = 128


def build_flow_card_visuals() -> dict[str, dict[str, Any]]:
    """Return the SmartEVSE visual definitions used by the flow card."""
    return {
        "off": {
            "color": [148, 163, 184],
            "fx": 0,
            "sx": 0,
            "ix": 0,
        },
        "idle": {
            "color": COLOR_IDLE,
            "fx": IDLE_FX,
            "sx": IDLE_SX,
            "ix": IDLE_IX,
        },
        "error": {
            "color": COLOR_ERROR,
            "fx": 2,
            "sx": 60,
            "ix": 200,
        },
        "charging": {
            "color": COLOR_CHARGING,
            "fx": CHARGING_FX,
            "sx": CHARGING_SX,
            "ix": CHARGING_IX,
            "pal": CHARGING_PAL,
            "c1": CHARGING_C1,
            "c2": CHARGING_C2,
            "c3": CHARGING_C3,
        },
    }


class WLEDPresetError(RuntimeError):
    """Raised when WLED assets cannot be recreated."""


@dataclass(frozen=True, slots=True)
class WLEDLayout:
    """WLED physical layout configuration."""

    led_count: int = DEFAULT_WLED_LED_COUNT
    led_offset: int = DEFAULT_WLED_LED_OFFSET

    @property
    def segment_split(self) -> int:
        """Return the midpoint used for the two half-circle segments."""
        return (self.led_count + 1) // 2

    @property
    def right_segment(self) -> tuple[int, int]:
        """Return the logical right-half segment."""
        return (0, self.segment_split)

    @property
    def left_segment(self) -> tuple[int, int]:
        """Return the logical left-half segment."""
        return (self.segment_split, self.led_count)


def normalize_wled_base_url(base_url: str) -> str:
    """Normalize a WLED URL to its base path."""
    normalized = base_url.strip()
    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"

    for suffix in ("/json/state", "/json", "/presets.json", "/upload"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break

    return normalized.rstrip("/") + "/"


def normalize_wled_state_url(base_url: str) -> str:
    """Return the JSON state endpoint for the configured WLED URL."""
    return urljoin(normalize_wled_base_url(base_url), "json/state")


def build_runtime_payload(
    *,
    smartevse_1: Any,
    smartevse_2: Any,
    led_count: int = DEFAULT_WLED_LED_COUNT,
    led_offset: int = DEFAULT_WLED_LED_OFFSET,
) -> dict[str, Any]:
    """Build the live WLED payload for both SmartEVSE segments."""
    layout = WLEDLayout(led_count=led_count, led_offset=led_offset)
    segments = [
        _segment_for_smartevse_status("smartevse_2", smartevse_2, layout),
        _segment_for_smartevse_status("smartevse_1", smartevse_1, layout),
    ]

    if not getattr(smartevse_1, "connected", False) and not getattr(smartevse_2, "connected", False):
        return {
            "on": False,
            "transition": 30,
            "ledmap": WLED_LED_MAP_ID,
            "seg": segments,
        }

    return {
        "on": True,
        "bri": 128,
        "transition": 7,
        "mainseg": 0,
        "ledmap": WLED_LED_MAP_ID,
        "seg": segments,
    }


def runtime_state_matches_payload(current_state: dict[str, Any], expected_payload: dict[str, Any]) -> bool:
    """Return whether the live WLED state already matches the managed runtime payload."""
    if bool(current_state.get("on")) != bool(expected_payload.get("on")):
        return False
    if int(current_state.get("ledmap", -1)) != int(expected_payload.get("ledmap", -1)):
        return False

    expected_on = bool(expected_payload.get("on"))
    if expected_on:
        if int(current_state.get("mainseg", -1)) != int(expected_payload.get("mainseg", -1)):
            return False
        if int(current_state.get("bri", -1)) != int(expected_payload.get("bri", -1)):
            return False

    current_segments_raw = current_state.get("seg") or []
    if isinstance(current_segments_raw, dict):
        current_segments = [current_segments_raw]
    elif isinstance(current_segments_raw, list):
        current_segments = current_segments_raw
    else:
        current_segments = []
    current_by_id = {
        int(segment.get("id", -1)): segment
        for segment in current_segments
        if isinstance(segment, dict) and "id" in segment
    }

    for expected_segment in expected_payload.get("seg", []):
        if not isinstance(expected_segment, dict):
            return False
        segment_id = int(expected_segment.get("id", -1))
        current_segment = current_by_id.get(segment_id)
        if current_segment is None:
            return False
        if not _segment_matches(current_segment, expected_segment):
            return False

    return True


async def async_recreate_wled_assets(
    hass: HomeAssistant,
    wled_url: str,
    *,
    led_count: int = DEFAULT_WLED_LED_COUNT,
    led_offset: int = DEFAULT_WLED_LED_OFFSET,
    presets_payload: dict[str, Any] | None = None,
) -> None:
    """Delete and recreate all WLED segments, presets, and LED map."""
    session = async_get_clientsession(hass)
    base_url = normalize_wled_base_url(wled_url)
    layout = WLEDLayout(led_count=led_count, led_offset=led_offset)

    info = await _async_get_json(session, urljoin(base_url, "json/info"))
    led_count = int((info.get("leds") or {}).get("count") or 0)
    if led_count and led_count != layout.led_count:
        raise WLEDPresetError(
            f"WLED reports {led_count} LEDs, but this preset layout requires {layout.led_count} LEDs"
        )

    state = await _async_get_json(session, urljoin(base_url, "json/state"))
    await _async_get_json(session, urljoin(base_url, "presets.json"))

    await _async_upload_json_file(
        session,
        urljoin(base_url, "upload"),
        "ledmap.json",
        _build_ledmap_payload(layout),
    )
    await _async_post_json(
        session,
        urljoin(base_url, "json/state"),
        _build_segment_wipe_payload(state, layout),
    )
    await _async_upload_json_file(
        session,
        urljoin(base_url, "upload"),
        "presets.json",
        _empty_presets_payload(),
    )
    await _async_upload_json_file(
        session,
        urljoin(base_url, "upload"),
        "presets.json",
        _build_presets_payload(layout, presets_payload=presets_payload),
    )
    await _async_post_json(
        session,
        urljoin(base_url, "json/state"),
        _build_segment_setup_payload(state, layout),
    )


def build_default_presets_json(
    *,
    led_count: int = DEFAULT_WLED_LED_COUNT,
    led_offset: int = DEFAULT_WLED_LED_OFFSET,
) -> str:
    """Return a pretty-printed default presets.json payload."""
    payload = _build_presets_payload(
        WLEDLayout(led_count=led_count, led_offset=led_offset),
        presets_payload=None,
    )
    return json.dumps(payload, indent=2, sort_keys=True)


def _segment_for_smartevse_status(smartevse_key: str, status: Any, layout: WLEDLayout) -> dict[str, Any]:
    """Return the runtime segment payload for one SmartEVSE."""
    if not getattr(status, "connected", False):
        return _segment_visual(smartevse_key, "off", layout)
    if _has_error(status):
        return _segment_visual(smartevse_key, "error", layout)
    state = str(getattr(status, "state", "") or "")
    if state == "Charging":
        return _segment_visual(smartevse_key, "charging", layout)
    return _segment_visual(smartevse_key, "idle", layout)


def _has_error(status: Any) -> bool:
    """Return whether the SmartEVSE should be shown as errored."""
    if not getattr(status, "connected", False):
        return False
    error = str(getattr(status, "error", "") or "")
    return error not in {"NONE", "None", "unknown", "unavailable", ""}


def _smartevse_segment(smartevse_key: str, layout: WLEDLayout) -> tuple[int, int, int]:
    """Return the WLED segment geometry for one SmartEVSE."""
    if smartevse_key == "smartevse_1":
        return 0, *layout.right_segment
    return 1, *layout.left_segment


def _build_ledmap_payload(layout: WLEDLayout) -> dict[str, Any]:
    """Build the rotated WLED LED map."""
    return {
        "map": [
            (index + layout.led_offset) % layout.led_count
            for index in range(layout.led_count)
        ]
    }


def _build_segment_setup_payload(state: dict[str, Any], layout: WLEDLayout) -> dict[str, Any]:
    """Return the WLED state payload that resets segments to the SmartEVSE layout."""
    existing_segments = state.get("seg") or []
    if isinstance(existing_segments, dict):
        segment_count = 1
    elif isinstance(existing_segments, list):
        segment_count = len(existing_segments)
    else:
        segment_count = 0

    segments = [
        {
            "id": 0,
            "start": layout.right_segment[0],
            "stop": layout.right_segment[1],
            "grp": 1,
            "spc": 0,
            "on": False,
            "sel": True,
        },
        {
            "id": 1,
            "start": layout.left_segment[0],
            "stop": layout.left_segment[1],
            "grp": 1,
            "spc": 0,
            "on": False,
            "sel": False,
        },
    ]
    segments.extend({"id": segment_id, "stop": 0} for segment_id in range(2, segment_count))

    return {
        "tt": 0,
        "mainseg": 0,
        "ledmap": WLED_LED_MAP_ID,
        "seg": segments,
    }


def _build_segment_wipe_payload(state: dict[str, Any], layout: WLEDLayout) -> dict[str, Any]:
    """Return the WLED state payload that removes all existing segments."""
    existing_segments = state.get("seg") or []
    if isinstance(existing_segments, dict):
        segment_count = 1
    elif isinstance(existing_segments, list):
        segment_count = len(existing_segments)
    else:
        segment_count = 0

    return {
        "tt": 0,
        "mainseg": 0,
        "ledmap": WLED_LED_MAP_ID,
        "seg": [{"id": segment_id, "stop": 0} for segment_id in range(segment_count)],
    }


def _empty_presets_payload() -> dict[str, Any]:
    """Return an empty preset file payload."""
    return {"0": {}}


def _build_presets_payload(
    layout: WLEDLayout,
    presets_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the SmartEVSE-managed preset set after a full wipe."""
    if presets_payload is not None:
        return presets_payload

    presets = _empty_presets_payload()
    taken_ids: set[int] = set()
    preset_defs = [
        (f"{WLED_PRESET_NAME_PREFIX}Off", _off_preset(layout)),
        (f"{WLED_PRESET_NAME_PREFIX}Error", _error_preset(layout)),
        (f"{WLED_PRESET_NAME_PREFIX}1 Charging", _combined_preset("charging", "off", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}1 Idle", _combined_preset("idle", "off", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}2 Charging", _combined_preset("off", "charging", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}2 Idle", _combined_preset("off", "idle", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}1 Idle + SmartEVSE 2 Idle", _combined_preset("idle", "idle", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}1 Charging + SmartEVSE 2 Idle", _combined_preset("charging", "idle", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}1 Idle + SmartEVSE 2 Charging", _combined_preset("idle", "charging", layout)),
        (f"{WLED_PRESET_NAME_PREFIX}1 Charging + SmartEVSE 2 Charging", _combined_preset("charging", "charging", layout)),
    ]
    preset_ids = _allocate_preset_ids(taken_ids, len(preset_defs))

    for preset_id, (name, payload) in zip(preset_ids, preset_defs, strict=True):
        presets[str(preset_id)] = {"n": name, **payload}

    return presets


def _allocate_preset_ids(taken_ids: set[int], count: int) -> list[int]:
    """Return a contiguous block of free preset IDs."""
    for start in range(WLED_PRESET_ID_START, WLED_PRESET_ID_LIMIT - count + 2):
        ids = list(range(start, start + count))
        if all(preset_id not in taken_ids for preset_id in ids):
            return ids
    raise WLEDPresetError("No free WLED preset block is available for SmartEVSE presets")


def _base_preset_payload() -> dict[str, Any]:
    """Return common preset metadata."""
    return {
        "on": True,
        "bri": 128,
        "transition": 7,
        "mainseg": 0,
        "ledmap": WLED_LED_MAP_ID,
    }


def _off_preset(layout: WLEDLayout) -> dict[str, Any]:
    """Return a fully off preset."""
    return {
        "on": False,
        "transition": 7,
        "ledmap": WLED_LED_MAP_ID,
        "seg": [
            {"id": 0, "start": layout.right_segment[0], "stop": layout.right_segment[1], "on": False},
            {"id": 1, "start": layout.left_segment[0], "stop": layout.left_segment[1], "on": False},
        ],
    }


def _error_preset(layout: WLEDLayout) -> dict[str, Any]:
    """Return an all-red error preset."""
    return {
        **_base_preset_payload(),
        "bri": 200,
        "seg": [
            {
                "id": 0,
                "start": layout.right_segment[0],
                "stop": layout.right_segment[1],
                "on": True,
                "col": [COLOR_ERROR],
                "fx": 2,
                "sx": 60,
                "ix": 200,
            },
            {
                "id": 1,
                "start": layout.left_segment[0],
                "stop": layout.left_segment[1],
                "on": True,
                "col": [COLOR_ERROR],
                "fx": 2,
                "sx": 60,
                "ix": 200,
            },
        ],
    }


def _combined_preset(smartevse_1_visual: str, smartevse_2_visual: str, layout: WLEDLayout) -> dict[str, Any]:
    """Return a preset for both SmartEVSE segments at once."""
    segments = sorted(
        [
            _segment_visual("smartevse_1", smartevse_1_visual, layout),
            _segment_visual("smartevse_2", smartevse_2_visual, layout),
        ],
        key=lambda segment: int(segment["id"]),
    )
    return {**_base_preset_payload(), "seg": segments}


def _segment_visual(smartevse_key: str, visual: str, layout: WLEDLayout) -> dict[str, Any]:
    """Return the WLED payload for one SmartEVSE visual state."""
    segment_id, start, stop = _smartevse_segment(smartevse_key, layout)
    if visual == "off":
        return {"id": segment_id, "start": start, "stop": stop, "on": False}
    if visual == "error":
        return {
            "id": segment_id,
            "start": start,
            "stop": stop,
            "on": True,
            "col": [COLOR_ERROR],
            "fx": 2,
            "sx": 60,
            "ix": 200,
        }
    if visual == "charging":
        return {
            "id": segment_id,
            "start": start,
            "stop": stop,
            "on": True,
            "col": [COLOR_CHARGING],
            "fx": CHARGING_FX,
            "sx": CHARGING_SX,
            "ix": CHARGING_IX,
            "pal": CHARGING_PAL,
            "c1": CHARGING_C1,
            "c2": CHARGING_C2,
            "c3": CHARGING_C3,
            "rev": smartevse_key == "smartevse_1",
        }
    return {
        "id": segment_id,
        "start": start,
        "stop": stop,
        "on": True,
        "col": [COLOR_IDLE],
        "fx": IDLE_FX,
        "sx": IDLE_SX,
        "ix": IDLE_IX,
    }


def _segment_matches(current_segment: dict[str, Any], expected_segment: dict[str, Any]) -> bool:
    """Compare only the fields the runtime payload actually manages."""
    common_keys = ("id", "start", "stop", "on")
    if any(current_segment.get(key) != expected_segment.get(key) for key in common_keys):
        return False

    if not expected_segment.get("on"):
        return True

    managed_optional_keys = ("fx", "sx", "ix", "pal", "c1", "c2", "c3", "rev")
    for key in managed_optional_keys:
        if key in expected_segment and current_segment.get(key) != expected_segment.get(key):
            return False

    if "col" in expected_segment:
        current_col = current_segment.get("col")
        if current_col != expected_segment["col"]:
            return False

    return True


async def _async_get_json(session, url: str) -> dict[str, Any]:
    """Fetch JSON from WLED."""
    try:
        async with session.get(url, timeout=10) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    except (TimeoutError, ClientError, ValueError) as err:
        raise WLEDPresetError(f"Unable to read {url}: {err}") from err

    if not isinstance(payload, dict):
        raise WLEDPresetError(f"Unexpected JSON payload from {url}")
    return payload


async def _async_upload_json_file(session, url: str, filename: str, payload: dict[str, Any]) -> None:
    """Upload a JSON file to WLED."""
    form = FormData()
    form.add_field(
        "data",
        json.dumps(payload, separators=(",", ":")).encode(),
        filename=filename,
        content_type="application/json",
    )

    try:
        async with session.post(url, data=form, timeout=20) as response:
            response.raise_for_status()
            await response.text()
    except (TimeoutError, ClientError) as err:
        raise WLEDPresetError(f"Unable to upload {filename}: {err}") from err


async def _async_post_json(session, url: str, payload: dict[str, Any]) -> None:
    """POST JSON to WLED."""
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            response.raise_for_status()
            await response.text()
    except (TimeoutError, ClientError) as err:
        raise WLEDPresetError(f"Unable to apply WLED state change: {err}") from err
