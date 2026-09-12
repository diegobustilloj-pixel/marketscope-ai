import unittest

from polymarket_bot.combined_shadow_close import _finished


class CombinedShadowCloseTests(unittest.TestCase):
    def test_finished_only_after_run_leaves_running(self) -> None:
        self.assertFalse(_finished({"latest_run": {"status": "RUNNING"}}))
        self.assertTrue(_finished({"latest_run": {"status": "COMPLETE"}}))
        self.assertTrue(_finished({"latest_run": {"status": "ERROR"}}))


if __name__ == "__main__":
    unittest.main()
