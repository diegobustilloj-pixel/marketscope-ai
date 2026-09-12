import asyncio
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.app import _safety_monitor
from polymarket_bot.storage import SQLiteStore
from polymarket_bot.system_guard import prevent_system_sleep


class SafetyTests(unittest.TestCase):
    def test_database_size_limit_stops_collector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "guard.db"
            store = SQLiteStore(db_path)
            store.open()
            store.close()
            stop_event = asyncio.Event()
            state = {
                "stop_reason": None,
                "database_bytes": 0,
                "free_bytes": 0,
            }
            asyncio.run(
                _safety_monitor(
                    db_path=db_path,
                    stop_event=stop_event,
                    max_db_gb=0.000001,
                    min_free_gb=0,
                    state=state,
                )
            )
            self.assertTrue(stop_event.is_set())
            self.assertIn("BASE_ALCANZO_LIMITE", state["stop_reason"])

    def test_sleep_guard_is_safe_on_current_platform(self) -> None:
        with prevent_system_sleep(True) as active:
            self.assertIsInstance(active, bool)


if __name__ == "__main__":
    unittest.main()
