"""Exercise native Store's real save wrapper, with synthetic disk failures."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError

from custom_components.smartevse_dual_charger.storage import StrictStore


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = SimpleNamespace(state=CoreState.running, data={},
            async_add_executor_job=asyncio.to_thread,
            config=SimpleNamespace(config_dir=self.directory.name,
                path=lambda *parts: str(Path(self.directory.name).joinpath(*parts))))
        self.store = StrictStore(self.hass, 1, 'test.charging_costs')

    async def test_native_errors_propagate_instead_of_claiming_a_saved_ledger(self):
        for error in (WriteError('disk full'), SerializationError('invalid data')):
            with self.subTest(error=type(error).__name__):
                with patch.object(Store, '_async_write_data', AsyncMock(side_effect=error)):
                    with self.assertRaises(HomeAssistantError):
                        await self.store.async_save({'test': True})

    async def test_stop_does_not_acknowledge_a_deferred_save(self):
        self.hass.state = CoreState.stopping
        with self.assertRaises(HomeAssistantError):
            await self.store.async_save({'test': True})
        self.assertIsNone(self.store._data)

    async def test_invalid_existing_envelope_is_not_reset_or_overwritten(self):
        path = Path(self.store.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ('{}', 'null', 'invalid-json', '{"version": 1, "key": "other", "data": {}}'):
            with self.subTest(content=content):
                path.write_text(content)
                store = StrictStore(self.hass, 1, 'test.charging_costs')
                with self.assertRaises(HomeAssistantError):
                    await store.async_load()
                with self.assertRaises(HomeAssistantError):
                    await store.async_save({'replacement': True})
                self.assertEqual(path.read_text(), content)

    async def test_cancelled_writer_retains_lock_until_disk_worker_finishes(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        written = []

        async def write(data):
            if data['data']['value'] == 1:
                started.set()
                await finish.wait()
            written.append(data['data']['value'])

        with patch.object(Store, '_async_write_data', AsyncMock(side_effect=write)):
            first = asyncio.create_task(self.store.async_save({'value': 1}))
            await started.wait()
            first.cancel()
            second = asyncio.create_task(self.store.async_save({'value': 2}))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            self.assertEqual(written, [])
            self.assertFalse(second.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
        self.assertEqual(written, [1, 2])


if __name__ == '__main__':
    unittest.main()
