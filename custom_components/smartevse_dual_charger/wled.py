"""WLED helpers for SmartEVSE Dual Charger."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError, FormData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

WLED_LED_COUNT = 105
WLED_SEGMENT_SPLIT = 53
WLED_LED_OFFSET = 11
WLED_LED_MAP_ID = 0
WLED_PRESET_NAME_PREFIX = "SmartEVSE "
WLED_PRESET_ID_START = 101
WLED_PRESET_ID_LIMIT = 250

RIGHT_SEGMENT = (0, WLED_SEGMENT_SPLIT)
LEFT_SEGMENT = (WLED_SEGMENT_SPLIT, WLED_LED_COUNT)

COLOR_CHARGING = [0, 255, 0]
COLOR_IDLE = [0, 100, 255]
COLOR_ERROR = [255, 0, 0]

IDLE_FX = 2
IDLE_SX = 45
IDLE_IX = 128


class WLEDPresetError(RuntimeError):
    """Raised when WLED assets cannot be recreated."""


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


def build_runtime_payload(*, smartevse_1: Any, smartevse_2: Any) -> dict[str, Any]:
    """Build the live WLED payload for both SmartEVSE segments."""
    segments = [
        _segment_for_smartevse_status("smartevse_2", smartevse_2),
        _segment_for_smartevse_status("smartevse_1", smartevse_1),
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


async def async_recreate_wled_assets(hass: HomeAssistant, wled_url: str) -> None:
    """Recreate SmartEVSE-specific segments, presets, and LED map."""
    session = async_get_clientsession(hass)
    base_url = normalize_wled_base_url(wled_url)

    info = await _async_get_json(session, urljoin(base_url, "json/info"))
    led_count = int((info.get("leds") or {}).get("count") or 0)
    if led_count and led_count != WLED_LED_COUNT:
        raise WLEDPresetError(
            f"WLED reports {led_count} LEDs, but this preset layout requires {WLED_LED_COUNT} LEDs"
        )

    state = await _async_get_json(session, urljoin(base_url, "json/state"))
    presets = await _async_get_json(session, urljoin(base_url, "presets.json"))
    preserved = _preserve_non_smartevse_presets(presets)

    await _async_upload_json_file(
        session,
        urljoin(base_url, "upload"),
        "ledmap.json",
        _build_ledmap_payload(),
    )
    await _async_upload_json_file(
        session,
        urljoin(base_url, "upload"),
        "presets.json",
        _build_presets_payload(preserved),
    )
    await _async_post_json(
        session,
        urljoin(base_url, "json/state"),
        _build_segment_setup_payload(state),
    )


def _segment_for_smartevse_status(smartevse_key: str, status: Any) -> dict[str, Any]:
    """Return the runtime segment payload for one SmartEVSE."""
    if not getattr(status, "connected", False):
        return _segment_visual(smartevse_key, "off")
    if _has_error(status):
        return _segment_visual(smartevse_key, "error")
    state = str(getattr(status, "state", "") or "")
    if state == "Charging":
        return _segment_visual(smartevse_key, "charging")
    return _segment_visual(smartevse_key, "idle")


def _has_error(status: Any) -> bool:
    """Return whether the SmartEVSE should be shown as errored."""
    if not getattr(status, "connected", False):
        return False
    error = str(getattr(status, "error", "") or "")
    return error not in {"NONE", "None", "unknown", "unavailable", ""}


def _smartevse_segment(smartevse_key: str) -> tuple[int, int, int]:
    """Return the WLED segment geometry for one SmartEVSE."""
    if smartevse_key == "smartevse_1":
        return 0, *RIGHT_SEGMENT
    return 1, *LEFT_SEGMENT


def _build_ledmap_payload() -> dict[str, Any]:
    """Build the rotated WLED LED map."""
    return {
        "map": [
            (index + WLED_LED_OFFSET) % WLED_LED_COUNT
            for index in range(WLED_LED_COUNT)
        ]
    }


def _build_segment_setup_payload(state: dict[str, Any]) -> dict[str, Any]:
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
            "start": RIGHT_SEGMENT[0],
            "stop": RIGHT_SEGMENT[1],
            "grp": 1,
            "spc": 0,
            "on": False,
            "sel": True,
        },
        {
            "id": 1,
            "start": LEFT_SEGMENT[0],
            "stop": LEFT_SEGMENT[1],
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


def _preserve_non_smartevse_presets(presets: dict[str, Any]) -> dict[str, Any]:
    """Drop old SmartEVSE presets while preserving unrelated user presets."""
    preserved: dict[str, Any] = {"0": {}}
    for raw_id, preset in presets.items():
        if raw_id == "0":
            preserved["0"] = preset if isinstance(preset, dict) else {}
            continue
        if not isinstance(preset, dict):
            continue
        if str(preset.get("n", "")).startswith("SmartEVSE"):
            continue
        preserved[raw_id] = preset
    return preserved


def _build_presets_payload(preserved: dict[str, Any]) -> dict[str, Any]:
    """Merge preserved presets with the SmartEVSE-managed preset set."""
    presets = dict(preserved)
    taken_ids = {int(raw_id) for raw_id in presets if raw_id.isdigit()}
    preset_defs = [
        (f"{WLED_PRESET_NAME_PREFIX}Off", _off_preset()),
        (f"{WLED_PRESET_NAME_PREFIX}Error", _error_preset()),
        (f"{WLED_PRESET_NAME_PREFIX}1 Charging", _combined_preset("charging", "off")),
        (f"{WLED_PRESET_NAME_PREFIX}1 Idle", _combined_preset("idle", "off")),
        (f"{WLED_PRESET_NAME_PREFIX}2 Charging", _combined_preset("off", "charging")),
        (f"{WLED_PRESET_NAME_PREFIX}2 Idle", _combined_preset("off", "idle")),
        (f"{WLED_PRESET_NAME_PREFIX}1 Idle + SmartEVSE 2 Idle", _combined_preset("idle", "idle")),
        (f"{WLED_PRESET_NAME_PREFIX}1 Charging + SmartEVSE 2 Idle", _combined_preset("charging", "idle")),
        (f"{WLED_PRESET_NAME_PREFIX}1 Idle + SmartEVSE 2 Charging", _combined_preset("idle", "charging")),
        (f"{WLED_PRESET_NAME_PREFIX}1 Charging + SmartEVSE 2 Charging", _combined_preset("charging", "charging")),
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


def _off_preset() -> dict[str, Any]:
    """Return a fully off preset."""
    return {
        "on": False,
        "transition": 7,
        "ledmap": WLED_LED_MAP_ID,
        "seg": [
            {"id": 0, "start": RIGHT_SEGMENT[0], "stop": RIGHT_SEGMENT[1], "on": False},
            {"id": 1, "start": LEFT_SEGMENT[0], "stop": LEFT_SEGMENT[1], "on": False},
        ],
    }


def _error_preset() -> dict[str, Any]:
    """Return an all-red error preset."""
    return {
        **_base_preset_payload(),
        "bri": 200,
        "seg": [
            {
                "id": 0,
                "start": RIGHT_SEGMENT[0],
                "stop": RIGHT_SEGMENT[1],
                "on": True,
                "col": [COLOR_ERROR],
                "fx": 2,
                "sx": 60,
                "ix": 200,
            },
            {
                "id": 1,
                "start": LEFT_SEGMENT[0],
                "stop": LEFT_SEGMENT[1],
                "on": True,
                "col": [COLOR_ERROR],
                "fx": 2,
                "sx": 60,
                "ix": 200,
            },
        ],
    }


def _combined_preset(smartevse_1_visual: str, smartevse_2_visual: str) -> dict[str, Any]:
    """Return a preset for both SmartEVSE segments at once."""
    segments = sorted(
        [
            _segment_visual("smartevse_1", smartevse_1_visual),
            _segment_visual("smartevse_2", smartevse_2_visual),
        ],
        key=lambda segment: int(segment["id"]),
    )
    return {**_base_preset_payload(), "seg": segments}


def _segment_visual(smartevse_key: str, visual: str) -> dict[str, Any]:
    """Return the WLED payload for one SmartEVSE visual state."""
    segment_id, start, stop = _smartevse_segment(smartevse_key)
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
            "fx": 28,
            "sx": 100,
            "ix": 128,
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
