import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_trader_v015 as paper_v015
from polymarket_bot import v015


def active_contract():
    return {
        "selected_stratum": "LOW",
        "minimum_market_start_ms": 1_000,
        "maximum_eligible_markets": 500,
        "target_trades": 10,
        "order_size_shares": 5.0,
        "entry_cost_band": {
            "cost_min": 0.05,
            "cost_max": 0.10,
            "cost_min_inclusive": True,
            "cost_max_inclusive": False,
        },
        "confirmation_gates": {
            "minimum_trades": 10,
            "maximum_single_positive_trade_share": 0.35,
        },
    }


def candidate(index):
    return {
        "condition_id": f"id-{index}",
        "market_start_ms": 1_000 + index,
        "direction": 1,
        "chosen_side_cost_60": 0.08,
        "abs_twap_move_bps": 20.0,
        "market_response_ratio": 0.1,
    }


class V015ProtocolTests(unittest.TestCase):
    def test_frozen_template_is_valid_and_status_reads_zero_labels(self):
        template = v015.verify_template()
        status = v015.status()

        self.assertEqual(template["target_trades"], 10)
        self.assertEqual(template["maximum_eligible_markets"], 500)
        self.assertEqual(status["labels_read_by_status"], 0)
        self.assertIn(
            status["activation"]["status"],
            {
                "WAITING_V014_RESULT",
                "READY_TO_ACTIVATE",
                "ACTIVE",
                "NOT_APPLICABLE_V014_NO_CANDIDATE",
            },
        )

    def test_activation_binds_selection_and_next_five_minute_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            template_path = data / "template.json"
            result_path = data / "v014.json"
            active_path = data / "active.json"
            paper_path = root / "paper.py"
            template_path.write_text("{}", encoding="utf-8")
            paper_path.write_text("paper", encoding="utf-8")
            result_path.write_text(
                json.dumps(
                    {
                        "schema": "resultado_v014_development12",
                        "status": "DEVELOPMENT_CANDIDATE_SELECTED",
                        "created_at": "2026-08-13T15:17:43+00:00",
                        "selected_for_v015": "LOW",
                        "strata_results": {"LOW": {"qualifies_for_v015": True}},
                    }
                ),
                encoding="utf-8",
            )
            template = {
                **active_contract(),
                "cutoff_rule": "next_5m_after_v014_result",
                "severity": "strict",
                "direction": "same",
                "entry_cost_field": "chosen_side_cost_60",
                "cost_strata": {"LOW": active_contract()["entry_cost_band"]},
                "stopping_rule": "first_10_or_500",
                "if_less_than_target_at_maximum": "FAIL_INSUFFICIENT_FREQUENCY",
                "no_partial_peeking": True,
                "no_retuning": True,
                "no_threshold_rescue": True,
                "sources": {"source.txt": "hash"},
                "real_money": "BLOQUEADO",
            }

            with patch.multiple(
                v015,
                ROOT=root,
                TEMPLATE=template_path,
                RESULT_V014=result_path,
                ACTIVE=active_path,
                PAPER_TRADER=paper_path,
            ), patch.object(v015, "verify_template", return_value=template), patch.object(
                v015, "verify_active", side_effect=lambda: v015.load(active_path)
            ):
                active = v015.activate_if_ready()

        self.assertEqual(active["selected_stratum"], "LOW")
        self.assertEqual(active["minimum_market_start_ms"], 1_786_634_400_000)
        self.assertTrue(active["no_partial_peeking"])

    def test_incomplete_population_never_reads_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [candidate(index) for index in range(9)]
            result = Path(directory) / "missing-result.json"
            with patch.object(v015, "RESULT", result), patch.object(
                v015,
                "postcut_population",
                return_value=(active_contract(), rows, rows),
            ), patch.object(v015, "read_only_target_labels") as label_reader:
                completed = v015.evaluate_if_ready()

        self.assertFalse(completed)
        label_reader.assert_not_called()

    def test_frequency_failure_at_500_reads_zero_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.json"
            active_path = root / "active.json"
            active_path.write_text("{}", encoding="utf-8")
            scoped = [candidate(index) for index in range(500)]
            qualifying = scoped[:9]
            with patch.multiple(v015, RESULT=result, ACTIVE=active_path), patch.object(
                v015,
                "postcut_population",
                return_value=(active_contract(), scoped, qualifying),
            ), patch.object(v015, "read_only_target_labels") as label_reader:
                completed = v015.evaluate_if_ready()
                payload = json.loads(result.read_text(encoding="utf-8"))

        self.assertTrue(completed)
        self.assertEqual(payload["status"], "FAIL_INSUFFICIENT_FREQUENCY")
        self.assertEqual(payload["labels_read"], 0)
        label_reader.assert_not_called()

    def test_ready_population_reads_exactly_ten_labels_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.json"
            active_path = root / "active.json"
            active_path.write_text("{}", encoding="utf-8")
            rows = [candidate(index) for index in range(10)]
            labels = {row["condition_id"]: "UP" for row in rows}
            with patch.multiple(v015, RESULT=result, ACTIVE=active_path), patch.object(
                v015,
                "postcut_population",
                return_value=(active_contract(), rows, rows),
            ), patch.object(
                v015, "read_only_target_labels", return_value=labels
            ) as label_reader:
                completed = v015.evaluate_if_ready()
                payload = json.loads(result.read_text(encoding="utf-8"))

        self.assertTrue(completed)
        label_reader.assert_called_once()
        self.assertEqual(len(label_reader.call_args.args[0]), 10)
        self.assertEqual(payload["labels_read"], 10)
        self.assertEqual(len(payload["trades_detail"]), 10)

    def test_label_reader_rejects_any_count_other_than_ten(self):
        with self.assertRaises(RuntimeError):
            v015.read_only_target_labels(["one"])

    def test_paper_trader_caps_orders_at_ten_and_forces_real_money_zero(self):
        rows = [candidate(index) for index in range(12)]
        with patch.object(
            paper_v015.v015,
            "postcut_population",
            return_value=(active_contract(), rows, rows),
        ):
            active, target = paper_v015.target_rows()

        self.assertEqual(active["target_trades"], 10)
        self.assertEqual(len(target), 10)
        self.assertIn("CHECK(real_money=0)", paper_v015.DDL)


if __name__ == "__main__":
    unittest.main()
