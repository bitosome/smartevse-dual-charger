"""Optional schedule reminders, isolated from all equipment control.

The schedule helper owns its calendar/timezone rules. Its off-state next_event
is the next opening; get_schedule identifies the whole active window so adjacent
blocks and a Home Assistant restart cannot create another start reminder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timedelta
import math
import re
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_NOTIFY_ON_SCHEDULE_WINDOW,
    CONF_SCHEDULE_NOTIFICATION_SERVICE,
    CONF_SCHEDULE_NOTIFICATION_TARGETS,
    CONF_SCHEDULE_REMINDER_MINUTES,
    DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW,
    DEFAULT_SCHEDULE_REMINDER_MINUTES,
    LOGGER,
    STORAGE_KEY,
)
from .storage import StrictStore

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_RETRY_DELAY = timedelta(minutes=5)
_SERVICE_TIMEOUT = 15
_MAX_RECEIPTS = 64


def validate_notification_options(options: dict[str, Any]) -> None:
    """Reject non-notification actions and malformed native entity targets."""
    action = str(options.get(CONF_SCHEDULE_NOTIFICATION_SERVICE) or "").strip()
    targets = options.get(CONF_SCHEDULE_NOTIFICATION_TARGETS) or []
    if not action:
        return
    if not re.fullmatch(r"notify\.[a-z0-9_]+", action):
        raise ValueError("invalid_notification_service")
    if action == "notify.send_message" and (
        not targets or any(not re.fullmatch(r"notify\.[a-z0-9_]+", target) for target in targets)
    ):
        raise ValueError("notification_targets_required")


def event_time(state: State) -> datetime | None:
    """Read the schedule's own aware next-event timestamp, never infer local time."""
    raw = state.attributes.get("next_event")
    value = raw if isinstance(raw, datetime) else dt_util.parse_datetime(str(raw))
    return dt_util.as_utc(value) if value is not None and value.tzinfo is not None else None


def active_window_start(schedule: dict[str, Any], now: datetime) -> str | None:
    """Identify a continuous active window using the native weekly definition.

    HA interprets these as local wall-clock ranges and time.max as the next
    midnight. Merge touching blocks, including across midnight and weekdays.
    Their opening instant stays stable through restart or an edited closing time.
    """
    ranges: dict[int, list[tuple[time, time]]] = {}
    for day, name in enumerate(_WEEKDAYS):
        ranges[day] = []
        for block in schedule.get(name, []):
            start = block["from"]
            end = block["to"]
            start = start if isinstance(start, time) else time.fromisoformat(start)
            end = end if isinstance(end, time) else time.fromisoformat(end)
            if start.tzinfo is not None or end.tzinfo is not None or start >= end:
                raise ValueError("Invalid native schedule range")
            ranges[day].append((start, end))

    # A continuously-on weekly helper has no finite close; keep a stable key.
    if all(_covers_day(blocks) for blocks in ranges.values()):
        return "continuous"

    local_now = dt_util.as_local(now)
    intervals: list[tuple[datetime, datetime]] = []
    for offset in range(-7, 8):
        day = local_now.date() + timedelta(days=offset)
        for start, end in ranges[day.weekday()]:
            first = datetime.combine(day, start, tzinfo=local_now.tzinfo)
            last = (
                datetime.combine(day + timedelta(days=1), time(), tzinfo=local_now.tzinfo)
                if end == time.max
                else datetime.combine(day, end, tzinfo=local_now.tzinfo)
            )
            intervals.append((first, last))
    merged: list[tuple[datetime, datetime]] = []
    for first, last in sorted(intervals):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    for first, last in merged:
        if first <= local_now < last:
            return dt_util.as_utc(first).isoformat()
    return None


def schedule_intervals(schedule: dict[str, Any], start: datetime, end: datetime, tz: Any) -> list[tuple[datetime, datetime]]:
    """The intervals (UTC) in which the weekly schedule is on between ``start`` and
    ``end``: local wall-clock blocks, touching blocks merged, also across midnight.
    Raises ValueError for a definition that is not a native weekly schedule."""
    if not isinstance(schedule, dict):
        raise ValueError("Invalid native schedule definition")
    first_day = dt_util.as_utc(start).astimezone(tz).date() - timedelta(days=1)
    last_day = dt_util.as_utc(end).astimezone(tz).date() + timedelta(days=1)
    found: list[tuple[datetime, datetime]] = []
    day = first_day
    while day <= last_day:
        for block in schedule.get(_WEEKDAYS[day.weekday()], []) or []:
            opens, closes = block["from"], block["to"]
            opens = opens if isinstance(opens, time) else time.fromisoformat(opens)
            if isinstance(closes, str) and closes.startswith("24:"):
                closes = time.max
            closes = closes if isinstance(closes, time) else time.fromisoformat(closes)
            if opens.tzinfo is not None or closes.tzinfo is not None or opens >= closes:
                raise ValueError("Invalid native schedule range")
            begin = datetime.combine(day, opens, tzinfo=tz)
            # HA writes the end of the day as time.max or 24:00, read back as 00:00.
            finish = (datetime.combine(day + timedelta(days=1), time(), tzinfo=tz) if closes == time.max
                      else datetime.combine(day, closes, tzinfo=tz))
            found.append((dt_util.as_utc(begin), dt_util.as_utc(finish)))
        day += timedelta(days=1)
    merged: list[tuple[datetime, datetime]] = []
    for begin, finish in sorted(found):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(finish, merged[-1][1]))
        else:
            merged.append((begin, finish))
    low, high = dt_util.as_utc(start), dt_util.as_utc(end)
    return [(max(begin, low), min(finish, high)) for begin, finish in merged if finish > low and begin < high]


def _covers_day(blocks: list[tuple[time, time]]) -> bool:
    end = time()
    for first, last in sorted(blocks):
        if first > end:
            return False
        end = max(end, last)
    return end == time.max


class ScheduleReminders:
    """Send at most one successful advance/start reminder per schedule window.

    Delivery runs outside the charging cycle. A durable claim precedes the
    notify action: a restart during an uncertain delivery does not repeat it.
    Definite failures release that claim and retry after five minutes while the
    reminder still applies. No overdue advance notices are sent after opening.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        schedule_entity: str | None,
        options: dict[str, Any],
        schedule_enabled: Callable[[], bool],
    ) -> None:
        self.hass = hass
        self._schedule_entity = schedule_entity
        self._options = options
        self._schedule_enabled = schedule_enabled
        self._store = StrictStore(hass, 1, f"{STORAGE_KEY}.{entry_id}.schedule_reminders")
        self._data: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._retry_at: datetime | None = None
        self._window_cache: tuple[datetime | None, str] | None = None
        self._closed = False

    async def async_initialize(self) -> None:
        """Load receipts, preserving unreadable data and disabling only reminders."""
        try:
            validate_notification_options(self._options)
            data = await self._store.async_load()
            if data is not None and (
                not isinstance(data, dict)
                or not isinstance(data.get("delivered"), list)
                or any(not isinstance(item, str) for item in data["delivered"])
            ):
                raise ValueError("Invalid schedule reminder receipts")
            self._data = data or {"delivered": []}
        except Exception:
            LOGGER.exception("Schedule reminder storage/options unavailable; charging control is unaffected")

    def _enabled(self) -> bool:
        return bool(
            not self._closed
            and self._data is not None
            and self._options.get(CONF_NOTIFY_ON_SCHEDULE_WINDOW, DEFAULT_NOTIFY_ON_SCHEDULE_WINDOW)
            and self._options.get(CONF_SCHEDULE_NOTIFICATION_SERVICE)
            and not self._schedule_enabled()
        )

    @callback
    def async_check(self) -> None:
        """Queue an independent check without waiting for a notification provider."""
        if not self._enabled() or (self._task is not None and not self._task.done()):
            return
        if self._retry_at is not None and dt_util.utcnow() < self._retry_at:
            return
        self._task = self.hass.async_create_background_task(
            self._async_check(), "SmartEVSE schedule reminder"
        )

    async def async_shutdown(self) -> None:
        """Cancel in-flight work before unloading the integration."""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _async_check(self) -> None:
        try:
            await self._async_notify_if_due()
        except Exception:
            self._retry_at = dt_util.utcnow() + _RETRY_DELAY
            LOGGER.exception("Schedule reminder failed; will retry if still applicable. Charging control is unaffected")

    async def _async_notify_if_due(self) -> None:
        state = self.hass.states.get(self._schedule_entity) if self._schedule_entity else None
        if not self._enabled() or state is None or state.state not in {"on", "off"}:
            return
        now = dt_util.utcnow()
        next_event = event_time(state)
        if state.state == "off":
            self._window_cache = None
            lead = max(0, int(self._options.get(CONF_SCHEDULE_REMINDER_MINUTES, DEFAULT_SCHEDULE_REMINDER_MINUTES)))
            if not lead or next_event is None or not now < next_event <= now + timedelta(minutes=lead):
                return
            key = f"advance:{self._schedule_entity}:{next_event.isoformat()}"
            minutes = math.ceil((next_event - now).total_seconds() / 60)
            start = dt_util.as_local(next_event).strftime("%a %H:%M")
            message = (
                f'Your charging schedule starts in {minutes} minute{"s" if minutes != 1 else ""} ({start}), '
                'but "Charge with schedule" is off. Enable it to use the scheduled charging window.'
            )
        else:
            # Read native calendar only when the active block changes. Its actual
            # window start is stable through restart and touching blocks.
            if self._window_cache is None or self._window_cache[0] != next_event:
                async with asyncio.timeout(_SERVICE_TIMEOUT):
                    response = await self.hass.services.async_call(
                        "schedule", "get_schedule", {"entity_id": self._schedule_entity},
                        blocking=True, return_response=True,
                    )
                definition = (response or {}).get(self._schedule_entity)
                if not isinstance(definition, dict):
                    raise ValueError("Native schedule definition is unavailable")
                start = active_window_start(definition, now)
                if start is None:
                    return  # State and definition changed during the read; retry next cycle.
                self._window_cache = (next_event, start)
            key = f"start:{self._schedule_entity}:{self._window_cache[1]}"
            message = (
                'Your charging schedule window is open, but "Charge with schedule" is off. '
                'Enable it to use the rest of this charging window.'
            )

        assert self._data is not None
        if key in self._data["delivered"]:
            return
        # Recheck after I/O, since a user may have enabled charging or changed
        # the schedule while the service/storage operation was in flight.
        if not self._still_due(state, key):
            return
        claim = {"delivered": [*self._data["delivered"], key][-_MAX_RECEIPTS:]}
        await self._store.async_save(claim)
        self._data = claim
        if not self._still_due(state, key):
            await self._release_claim(key)
            return
        action = str(self._options[CONF_SCHEDULE_NOTIFICATION_SERVICE]).strip().split(".", 1)[1]
        targets = self._options.get(CONF_SCHEDULE_NOTIFICATION_TARGETS) or []
        payload: dict[str, Any] = {"title": "EV charge schedule", "message": message}
        if targets:
            payload["entity_id" if action == "send_message" else "target"] = targets
        try:
            async with asyncio.timeout(_SERVICE_TIMEOUT):
                await self.hass.services.async_call("notify", action, payload, blocking=True)
        except TimeoutError:
            # The provider may already have sent it. Keep the claim on timeout
            # instead of producing duplicate notifications after an uncertain send.
            LOGGER.warning("Schedule notification timed out; delivery is uncertain and will not be repeated for this window")
        except Exception:
            await self._release_claim(key)
            raise
        self._retry_at = None

    def _still_due(self, original: State, key: str) -> bool:
        current = self.hass.states.get(self._schedule_entity)
        if not self._enabled() or current is None or current.state != original.state:
            return False
        if current.attributes.get("next_event") != original.attributes.get("next_event"):
            return False
        event = event_time(current)
        if key.startswith("advance:"):
            return event is not None and dt_util.utcnow() < event
        return event is None or dt_util.utcnow() < event

    async def _release_claim(self, key: str) -> None:
        assert self._data is not None
        released = {"delivered": [item for item in self._data["delivered"] if item != key]}
        await self._store.async_save(released)
        self._data = released
