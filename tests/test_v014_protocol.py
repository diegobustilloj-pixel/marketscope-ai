import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_trader_v014 as paper
import v014_monitor as protocol


def _prereg(*, maximum=250):
    return {
        "forward_after_market_start_ms": 1_000,
        "maximum_eligible_markets": maximum,
        "target_per_stratum": 6,
        "order_size_shares": 5.0,
        "strata": {
            "LOW": {
                "cost_min": 0.05,
                "cost_min_inclusive": True,
                "cost_max": 0.10,
                "cost_max_inclusive": False,
            },
            "MODERATE": {
                "cost_min": 0.22,
                "cost_min_inclusive": False,
                "cost_max": 0.30,
                "cost_max_inclusive": True,
            },
        },
        "development_gates_per_stratum": {
            "minimum_trades": 6,
            "minimum_wins": 2,
            "net_pnl": "positive",
            "roi_on_cost": "positive",
            "first_half_net_pnl": "nonnegative",
            "second_half_net_pnl": "nonnegative",
            "maximum_single_positive_trade_share": 0.60,
        },
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


def _row(condition_id, start, cost, *, shock=3.0, response=0.25, direction=1):
    return {
        "condition_id": condition_id,
        "market_start_ms": start,
        "chosen_side_cost_60": cost,
        "abs_twap_move_bps": shock,
        "market_response_ratio": response,
        "direction": direction,
    }


class V014ClassificationTests(unittest.TestCase):
    def test_strata_boundaries_are_disjoint_from_v013(self):
        prereg = _prereg()
        cases = {
            0.049999: None,
            0.05: "LOW",
            0.099999: "LOW",
            0.10: None,
            0.15: None,
            0.22: None,
            0.220001: "MODERATE",
            0.30: "MODERATE",
            0.300001: None,
        }
        for cost, expected in cases.items():
            with self.subTest(cost=cost):
                self.assertEqual(protocol.classify_cost(cost, prereg), expected)

    def test_population_applies_cutoff_strict_filters_scope_and_strata(self):
        rows = [
            _row("at-cutoff", 1_000, 0.06),
            _row("low", 1_001, 0.06),
            _row("v013", 1_002, 0.15),
            _row("moderate", 1_003, 0.25),
            _row("weak-shock", 1_004, 0.06, shock=1.99),
            _row("late-outside-scope", 1_005, 0.25),
        ]
        with (
            patch.object(
                protocol,
                "verify_frozen",
                return_value=(_prereg(maximum=4), _thresholds(), {}),
            ),
            patch.object(protocol.cal, "eligible", return_value=rows),
        ):
            scoped, strata = protocol.postcut_population()

        self.assertEqual(
            [row["condition_id"] for row in scoped],
            ["low", "v013", "moderate", "weak-shock"],
        )
        self.assertEqual([row["condition_id"] for row in strata["LOW"]], ["low"])
        self.assertEqual(
            [row["condition_id"] for row in strata["MODERATE"]],
            ["moderate"],
        )


class V014NoPeekingTests(unittest.TestCase):
    def test_not_ready_never_reads_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"
            with (
                patch.object(protocol, "RESULT", result),
                patch.object(
                    protocol,
                    "verify_frozen",
                    return_value=(_prereg(), _thresholds(), {}),
                ),
                patch.object(
                    protocol,
                    "postcut_population",
                    return_value=([], {"LOW": [], "MODERATE": []}),
                ),
                patch.object(
                    protocol,
                    "read_only_target_labels",
                    side_effect=AssertionError("labels must remain sealed"),
                ) as labels,
            ):
                ready = protocol.evaluate_if_ready()

            self.assertFalse(ready)
            self.assertFalse(result.exists())
            labels.assert_not_called()

    def test_frequency_failure_reads_zero_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"
            scoped = [_row(f"e{i}", 2_000 + i, 0.15) for i in range(3)]
            with (
                patch.object(protocol, "RESULT", result),
                patch.object(
                    protocol,
                    "verify_frozen",
                    return_value=(_prereg(maximum=3), _thresholds(), {}),
                ),
                patch.object(
                    protocol,
                    "postcut_population",
                    return_value=(
                        scoped,
                        {"LOW": [_row("low", 2_001, 0.06)], "MODERATE": []},
                    ),
                ),
                patch.object(
                    protocol,
                    "read_only_target_labels",
                    side_effect=AssertionError("labels must remain sealed"),
                ) as labels,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                ready = protocol.evaluate_if_ready()

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertTrue(ready)
            self.assertEqual(payload["status"], "FAIL_INSUFFICIENT_FREQUENCY")
            self.assertEqual(payload["labels_read"], 0)
            self.assertFalse(payload["pnl_calculated"])
            labels.assert_not_called()

    def test_label_reader_requires_exactly_twelve_unique_ids(self):
        with patch.object(
            protocol.cal,
            "ro",
            side_effect=AssertionError("database must remain closed"),
        ) as database:
            for ids in (["x"] * 11, ["x"] * 12, [str(i) for i in range(13)]):
                with self.subTest(size=len(ids), unique=len(set(ids))):
                    with self.assertRaises(RuntimeError):
                        protocol.read_only_target_labels(ids)
        database.assert_not_called()

    def test_ready_evaluation_opens_first_six_per_stratum_only(self):
        low = [_row(f"low{i}", 2_000 + i * 2, 0.06) for i in range(7)]
        moderate = [_row(f"mod{i}", 2_001 + i * 2, 0.25) for i in range(7)]
        expected_ids = {row["condition_id"] for row in low[:6] + moderate[:6]}
        labels = {
            condition_id: ("UP" if condition_id in {"low0", "low3", "low4"} else "DOWN")
            for condition_id in expected_ids
        }

        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"
            captured = []

            def read_labels(ids):
                captured.extend(ids)
                return labels

            with (
                patch.object(protocol, "RESULT", result),
                patch.object(
                    protocol,
                    "verify_frozen",
                    return_value=(_prereg(), _thresholds(), {}),
                ),
                patch.object(
                    protocol,
                    "postcut_population",
                    return_value=(low + moderate, {"LOW": low, "MODERATE": moderate}),
                ),
                patch.object(protocol, "read_only_target_labels", side_effect=read_labels),
                patch.object(protocol, "assert_no_v013_candidate_overlap") as isolation,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                ready = protocol.evaluate_if_ready()

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertTrue(ready)
            self.assertEqual(len(captured), 12)
            self.assertEqual(set(captured), expected_ids)
            self.assertEqual(payload["labels_read"], 12)
            self.assertEqual(payload["v013_candidate_outcomes_read"], 0)
            self.assertEqual(payload["selected_for_v015"], "LOW")
            isolation.assert_called_once()
            self.assertEqual(set(isolation.call_args.args[0]), expected_ids)
            self.assertTrue(
                payload["strata_results"]["LOW"]["qualifies_for_v015"]
            )
            self.assertFalse(
                payload["strata_results"]["MODERATE"]["qualifies_for_v015"]
            )

    def test_runtime_overlap_guard_aborts_before_labels(self):
        with (
            patch.object(
                protocol.v013,
                "postcut_population",
                return_value=([], [{"condition_id": "shared"}]),
            ),
            patch.object(
                protocol,
                "read_only_target_labels",
                side_effect=AssertionError("labels must remain sealed"),
            ) as labels,
        ):
            with self.assertRaises(RuntimeError):
                protocol.assert_no_v013_candidate_overlap(
                    ["shared"] + [f"id{i}" for i in range(11)]
                )
        labels.assert_not_called()


class PaperTraderV014Tests(unittest.TestCase):
    def test_paper_targets_are_limited_to_six_per_stratum(self):
        low = [_row(f"low{i}", 2_000 + i * 2, 0.06) for i in range(7)]
        moderate = [_row(f"mod{i}", 2_001 + i * 2, 0.25) for i in range(7)]
        with (
            patch.object(
                paper.protocol,
                "verify_frozen",
                return_value=(_prereg(), _thresholds(), {}),
            ),
            patch.object(
                paper.protocol,
                "postcut_population",
                return_value=(low + moderate, {"LOW": low, "MODERATE": moderate}),
            ),
        ):
            rows = paper.target_rows()

        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(stratum == "LOW" for stratum, _ in rows), 6)
        self.assertEqual(sum(stratum == "MODERATE" for stratum, _ in rows), 6)
        self.assertNotIn("low6", {row["condition_id"] for _, row in rows})
        self.assertNotIn("mod6", {row["condition_id"] for _, row in rows})


if __name__ == "__main__":
    unittest.main()
