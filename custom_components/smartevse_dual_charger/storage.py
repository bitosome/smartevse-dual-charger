"""HA-compatible storage that cannot acknowledge an unsuccessful safety save."""

import asyncio
from pathlib import Path

from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError, load_json


class StrictStore(Store):
    """Keep HA migration/cache semantics, but propagate durable-write failures.

    Native Store logs WriteError/SerializationError instead of raising them.
    Translating at its write hook lets the caller stop equipment commands when
    their safety state was not saved. This hook is covered against pinned HA.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **(kwargs | {"atomic_writes": True, "private": True}))
        self._strict_load_failed = False

    def _validate_envelope(self):
        # Native Store treats {} as a missing file and recovers malformed JSON by
        # renaming it and returning None. Safety holds and accounting history must
        # instead remain blocked until an operator restores valid storage.
        if not Path(self.path).exists():
            return
        data = load_json(self.path, None)
        if (
            not isinstance(data, dict)
            or data.get("key") != self.key
            or type(data.get("version")) is not int
            or type(data.get("minor_version", 1)) is not int
            or not isinstance(data.get("data"), dict)
        ):
            raise HomeAssistantError("Invalid persistent storage; restore a backup")

    async def async_load(self):
        try:
            await self.hass.async_add_executor_job(self._validate_envelope)
            return await super().async_load()
        except Exception:
            self._strict_load_failed = True
            raise

    async def async_save(self, data):
        if self._strict_load_failed:
            raise HomeAssistantError("Persistent storage could not be loaded; save refused")
        if self.hass.state in (CoreState.stopping, CoreState.stopped, CoreState.final_write):
            raise HomeAssistantError("Home Assistant is stopping; storage was not saved")
        await super().async_save(data)

    async def _async_write_data(self, data):
        # Keep Store's serialization lock until its disk worker has really exited.
        # Otherwise cancellation could let an older snapshot replace a later save.
        write = asyncio.create_task(super()._async_write_data(data))
        cancelled = False
        try:
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    cancelled = True
            write.result()
        except (WriteError, SerializationError) as err:
            raise HomeAssistantError("Persistent storage write failed") from err
        if cancelled:
            raise asyncio.CancelledError
