import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import execution_collector_v012 as execution
import v013_monitor as monitor


def _book(*, asks, bids=((0.19, 10.0),)):
    return {
        "asks": list(asks),
        "bids": list(bids),
        "book_timestamp_ms": 123456789,
    }


def _prereg(*, maximum=300):
    return {
        "forward_after_market_start_ms": 1_000,
        "max_eligible_markets": maximum,
        "entry_cost_band": [0.10, 0.22],
    }


def _thresholds():
    return {
        "thresholds": {
            "strict": {
                "shock_abs_bps_min": 2.0,
                "market_response_ratio_max": 0.50,
            }
        }
    }


def _eligible_row(
    condition_id,
    market_start_ms,
    *,
    cost=0.15,
    shock=3.0,
    response=0.25,
):
    return {
        "condition_id": condition_id,
        "market_start_ms": market_start_ms,
        "chosen_side_cost_60": cost,
        "abs_twap_move_bps": shock,
        "market_response_ratio": response,
        "direction": 1,
    }


class ExecutionCollectorV012Tests(unittest.TestCase):
    def test_vwap_uses_all_levels_and_never_beats_best_ask(self):
        result = execution.execution_metrics(
            _book(asks=((0.20, 2.0), (0.25, 3.0))),
            requested_size=5.0,
            fee_rate=0.0,
            taker_fee_enabled=0,
        )

        self.assertEqual(result["executable"], 1)
        self.assertAlmostEqual(result["vwap_buy"], 0.23)
        self.assertGreaterEqual(result["vwap_buy"], result["best_ask"])
        self.assertEqual(result["levels_consumed"], 2)

    def test_fee_is_added_to_total_cost_per_share(self):
        result = execution.execution_metrics(
            _book(asks=((0.20, 5.0),)),
            requested_size=5.0,
            fee_rate=0.0625,
            taker_fee_enabled=1,
        )

        self.assertAlmostEqual(result["fee_per_share"], 0.01)
        self.assertAlmostEqual(result["total_cost_per_share"], 0.21)
        self.assertAlmostEqual(result["profit_if_win_per_share"], 0.79)

    def test_insufficient_depth_has_no_simulated_fill(self):
        result = execution.execution_metrics(
            _book(asks=((0.20, 4.99),)),
            requested_size=5.0,
            fee_rate=0.0,
            taker_fee_enabled=0,
        )

        self.assertEqual(result["executable"], 0)
        self.assertIsNone(result["vwap_buy"])
        self.assertIsNone(result["total_cost_per_share"])

    def test_retryable_network_error_is_retried(self):
        with (
            patch.object(
                execution,
                "http_json",
                side_effect=[urllib.error.URLError("dns"), {"ok": True}],
            ) as request,
            patch.object(execution.time, "sleep") as sleep,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                result = execution.http_json_retry(
                    "https://example.invalid",
                    timeout=10.0,
                    attempts=3,
                    retry_delay_seconds=0.01,
                )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.01)
        self.assertEqual(request.call_args_list[0].args[1], 2.0)

    def test_nonretryable_http_error_is_not_retried(self):
        error = urllib.error.HTTPError(
            "https://example.invalid",
            404,
            "not found",
            hdrs=None,
            fp=None,
        )
        with (
            patch.object(execution, "http_json", side_effect=error) as request,
            patch.object(execution.time, "sleep") as sleep,
        ):
            with self.assertRaises(urllib.error.HTTPError):
                execution.http_json_retry(
                    "https://example.invalid",
                    timeout=1.0,
                    attempts=3,
                )

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        error.close()


class V013ProtocolTests(unittest.TestCase):
    def test_population_applies_cutoff_strict_rule_cost_band_and_scope(self):
        rows = [
            _eligible_row("before-cutoff", 1_000),
            _eligible_row("lower-bound", 1_001, cost=0.10),
            _eligible_row("weak-shock", 1_002, shock=1.99),
            _eligible_row("upper-bound", 1_003, cost=0.22, response=0.50),
            _eligible_row("outside-scope", 1_004),
        ]

        with (
            patch.object(
                monitor,
                "verify_spec",
                return_value=(_prereg(maximum=3), _thresholds(), {}),
            ),
            patch.object(monitor.cal, "eligible", return_value=rows),
        ):
            scoped, candidates = monitor.postcut_population()

        self.assertEqual(
            [row["condition_id"] for row in scoped],
            ["lower-bound", "weak-shock", "upper-bound"],
        )
        self.assertEqual(
            [row["condition_id"] for row in candidates],
            ["lower-bound", "upper-bound"],
        )

    def test_not_ready_does_not_read_labels_or_write_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "resultado.json"
            with (
                patch.object(monitor, "RESULT", result),
                patch.object(
                    monitor,
                    "verify_spec",
                    return_value=(_prereg(), _thresholds(), {}),
                ),
                patch.object(
                    monitor,
                    "postcut_population",
                    return_value=([_eligible_row("a", 1_001)], []),
                ),
                patch.object(
                    monitor,
                    "read_only_target_labels",
                    side_effect=AssertionError("labels must stay closed"),
                ) as labels,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    ready = monitor.evaluate_if_ready()

            self.assertFalse(ready)
            self.assertFalse(result.exists())
            labels.assert_not_called()

    def test_frequency_failure_at_limit_reads_zero_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "resultado.json"
            scoped = [
                _eligible_row("a", 1_001),
                _eligible_row("b", 1_002),
                _eligible_row("c", 1_003),
            ]
            with (
                patch.object(monitor, "RESULT", result),
                patch.object(
                    monitor,
                    "verify_spec",
                    return_value=(_prereg(maximum=3), _thresholds(), {}),
                ),
                patch.object(
                    monitor,
                    "postcut_population",
                    return_value=(scoped, scoped[:1]),
                ),
                patch.object(
                    monitor,
                    "read_only_target_labels",
                    side_effect=AssertionError("labels must stay closed"),
                ) as labels,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    ready = monitor.evaluate_if_ready()

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertTrue(ready)
            self.assertEqual(payload["status"], "FAIL_INSUFFICIENT_FREQUENCY")
            self.assertEqual(payload["labels_read"], 0)
            self.assertFalse(payload["pnl_calculated"])
            labels.assert_not_called()

    def test_label_reader_rejects_any_target_size_other_than_ten(self):
        with patch.object(
            monitor.cal,
            "ro",
            side_effect=AssertionError("database must not be opened"),
        ) as open_database:
            with self.assertRaises(RuntimeError):
                monitor.read_only_target_labels(["id"] * 9)

        open_database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
