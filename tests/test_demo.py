import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from polymarket_bot.app import run_demo
from polymarket_bot.config import Settings


class DemoTests(unittest.TestCase):
    def test_demo_creates_verified_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                Settings.from_env(),
                db_path=Path(directory) / "demo.db",
            )
            result = asyncio.run(run_demo(settings))
            self.assertEqual(result["events"], 5)
            self.assertEqual(result["markets"], 1)
            self.assertEqual(result["integrity"]["corrupt"], 0)
            self.assertEqual(
                result["mode"], "DEMO_SIN_INTERNET_SIN_DINERO"
            )


if __name__ == "__main__":
    unittest.main()

