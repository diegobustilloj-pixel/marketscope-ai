import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.paper_risk import (
    PROFILE_SCHEMA,
    PaperOrderProposal,
    PaperRiskEngine,
    PaperRiskState,
    RiskLimits,
    load_active_limits,
    paper_risk_status,
)


def _limits(**overrides):
    values = {
        "max_order_shares": 5.0,
        "max_order_cash": 1.5,
        "max_open_positions": 2,
        "max_open_cash": 2.0,
        "max_session_loss": 3.0,
        "max_drawdown": 2.0,
        "max_consecutive_losses": 2,
    }
    values.update(overrides)
    return RiskLimits(**values)


def _proposal(condition_id="market-1", **overrides):
    values = {
        "condition_id": condition_id,
        "strategy_id": "paper-test",
        "side": "UP",
        "entry_cost_per_share": 0.20,
        "shares": 5.0,
        "decision_timestamp_ms": 1_000,
        "executable": True,
        "data_fresh": True,
        "real_money": 0,
    }
    values.update(overrides)
    return PaperOrderProposal(**values)


def _active_profile(**limit_overrides):
    limits = {
        "max_order_shares": 5.0,
        "max_order_cash": 1.5,
        "max_open_positions": 2,
        "max_open_cash": 2.0,
        "max_session_loss": 3.0,
        "max_drawdown": 2.0,
        "max_consecutive_losses": 2,
    }
    limits.update(limit_overrides)
    return {
        "schema": PROFILE_SCHEMA,
        "status": "FROZEN_PAPER_ONLY",
        "controls": {
            "orders_enabled": False,
            "wallet_required": False,
            "private_api_required": False,
            "kelly_enabled": False,
            "leverage_enabled": False,
            "compounding_enabled": False,
        },
        "limits": limits,
    }


class RiskProfileTests(unittest.TestCase):
    def test_draft_profile_fails_closed_and_verifies_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("source", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            profile = root / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema": PROFILE_SCHEMA,
                        "status": "DRAFT_BLOCKED",
                        "controls": _active_profile()["controls"],
                        "derived_limits": {
                            "max_order_shares": 5.0,
                            "max_order_cash": 1.5,
                        },
                        "pending_limits": {
                            "max_open_positions": None,
                            "max_open_cash": None,
                            "max_session_loss": None,
                            "max_drawdown": None,
                            "max_consecutive_losses": None,
                        },
                        "sources": {"source.json": digest},
                    }
                ),
                encoding="utf-8",
            )

            status = paper_risk_status(profile, project_root=root)

        self.assertFalse(status["ready_for_paper_risk_engine"])
        self.assertIn("PROFILE_NOT_FROZEN_PAPER_ONLY", status["blockers"])
        self.assertIn("MISSING_LIMITS", status["blockers"])
        self.assertFalse(status["orders_enabled"])
        self.assertFalse(status["wallet_required"])
        self.assertTrue(status["source_checks"][0]["matches"])

    def test_profile_hash_mismatch_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("changed", encoding="utf-8")
            profile = root / "profile.json"
            payload = _active_profile()
            payload["sources"] = {"source.json": "0" * 64}
            profile.write_text(json.dumps(payload), encoding="utf-8")

            status = paper_risk_status(profile, project_root=root)

        self.assertFalse(status["ready_for_paper_risk_engine"])
        self.assertIn("SOURCE_HASH_MISMATCH", status["blockers"])

    def test_active_profile_with_invalid_limits_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("verified", encoding="utf-8")
            profile_path = root / "profile.json"
            profile = _active_profile(max_order_cash=-1.0)
            profile["sources"] = {
                "source.json": hashlib.sha256(source.read_bytes()).hexdigest()
            }
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            status = paper_risk_status(profile_path, project_root=root)

        self.assertFalse(status["ready_for_paper_risk_engine"])
        self.assertIn("INVALID_LIMITS", status["blockers"])
        self.assertIsNotNone(status["limit_validation_error"])

    def test_active_loader_requires_and_verifies_source_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "result.json"
            source.write_text("verified", encoding="utf-8")
            profile_path = root / "profile.json"
            profile = _active_profile()
            profile["sources"] = {
                "result.json": hashlib.sha256(source.read_bytes()).hexdigest()
            }
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            limits = load_active_limits(profile_path, project_root=root)

        self.assertEqual(limits.max_order_shares, 5.0)
        self.assertEqual(limits.max_open_positions, 2)

    def test_active_loader_rejects_profile_without_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.json"
            profile_path.write_text(
                json.dumps(_active_profile()),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_active_limits(profile_path, project_root=directory)

    def test_draft_cannot_create_active_limits(self):
        profile = _active_profile()
        profile["status"] = "DRAFT_BLOCKED"
        with self.assertRaises(ValueError):
            RiskLimits.from_profile(profile)

    def test_unsafe_control_cannot_create_active_limits(self):
        profile = _active_profile()
        profile["controls"]["kelly_enabled"] = True
        with self.assertRaises(ValueError):
            RiskLimits.from_profile(profile)

    def test_limits_require_consistent_positive_values(self):
        with self.assertRaises(ValueError):
            _limits(max_order_cash=0)
        with self.assertRaises(ValueError):
            _limits(max_open_cash=1.0)
        with self.assertRaises(ValueError):
            _limits(max_open_positions=0)


class PaperRiskEngineTests(unittest.TestCase):
    def test_safe_proposal_records_only_a_paper_position(self):
        engine = PaperRiskEngine(_limits())
        decision = engine.record_paper_fill(_proposal())

        self.assertTrue(decision.approved)
        self.assertEqual(decision.real_money, 0)
        self.assertEqual(len(engine.state.positions), 1)
        position = engine.state.positions["market-1"]
        self.assertEqual(position.real_money, 0)
        self.assertAlmostEqual(position.cash_outlay, 1.0)

    def test_real_money_stale_and_non_executable_are_rejected(self):
        engine = PaperRiskEngine(_limits())
        decision = engine.evaluate(
            _proposal(real_money=1, data_fresh=False, executable=False)
        )

        self.assertFalse(decision.approved)
        self.assertIn("REAL_MONEY_FORBIDDEN", decision.reasons)
        self.assertIn("STALE_DATA", decision.reasons)
        self.assertIn("NOT_EXECUTABLE", decision.reasons)
        self.assertEqual(engine.state.positions, {})

    def test_proposal_requires_strict_safety_types(self):
        with self.assertRaises(ValueError):
            _proposal(real_money=0.0)
        with self.assertRaises(ValueError):
            _proposal(executable=1)
        with self.assertRaises(ValueError):
            _proposal(data_fresh="yes")
        with self.assertRaises(ValueError):
            _proposal(decision_timestamp_ms=True)

    def test_per_order_share_and_cash_limits_are_enforced(self):
        engine = PaperRiskEngine(_limits())
        decision = engine.evaluate(
            _proposal(entry_cost_per_share=0.31, shares=5.1)
        )

        self.assertFalse(decision.approved)
        self.assertIn("MAX_ORDER_SHARES", decision.reasons)
        self.assertIn("MAX_ORDER_CASH", decision.reasons)

    def test_duplicate_position_and_portfolio_limits_are_enforced(self):
        engine = PaperRiskEngine(_limits())
        self.assertTrue(engine.record_paper_fill(_proposal("one")).approved)
        duplicate = engine.evaluate(_proposal("one"))
        self.assertIn("DUPLICATE_POSITION", duplicate.reasons)

        self.assertTrue(engine.record_paper_fill(_proposal("two")).approved)
        third = engine.evaluate(_proposal("three"))
        self.assertFalse(third.approved)
        self.assertIn("MAX_OPEN_POSITIONS", third.reasons)
        self.assertIn("MAX_OPEN_CASH", third.reasons)

    def test_losses_trigger_drawdown_and_consecutive_loss_gates(self):
        engine = PaperRiskEngine(_limits())
        for condition in ("one", "two"):
            self.assertTrue(
                engine.record_paper_fill(_proposal(condition)).approved
            )
            self.assertAlmostEqual(
                engine.settle_paper_position(condition, "DOWN"),
                -1.0,
            )

        decision = engine.evaluate(_proposal("three"))
        self.assertFalse(decision.approved)
        self.assertIn("MAX_DRAWDOWN", decision.reasons)
        self.assertIn("MAX_CONSECUTIVE_LOSSES", decision.reasons)

    def test_session_loss_gate_is_fail_closed(self):
        state = PaperRiskState(
            realized_pnl=-3.0,
            peak_realized_pnl=0.0,
        )
        engine = PaperRiskEngine(
            _limits(max_drawdown=10.0, max_consecutive_losses=10),
            state,
        )
        decision = engine.evaluate(_proposal())
        self.assertIn("MAX_SESSION_LOSS", decision.reasons)

    def test_open_and_proposed_risk_cannot_cross_loss_limits(self):
        engine = PaperRiskEngine(
            _limits(
                max_open_positions=3,
                max_open_cash=10.0,
                max_session_loss=1.5,
                max_drawdown=1.5,
            )
        )
        self.assertTrue(engine.record_paper_fill(_proposal("one")).approved)

        decision = engine.evaluate(_proposal("two"))

        self.assertFalse(decision.approved)
        self.assertIn("MAX_SESSION_LOSS", decision.reasons)
        self.assertIn("MAX_DRAWDOWN", decision.reasons)

    def test_manual_kill_switch_blocks_new_proposals(self):
        engine = PaperRiskEngine(_limits())
        engine.activate_kill_switch()
        decision = engine.evaluate(_proposal())
        self.assertFalse(decision.approved)
        self.assertIn("KILL_SWITCH", decision.reasons)

    def test_win_settlement_uses_binary_payout_without_compounding(self):
        engine = PaperRiskEngine(_limits())
        engine.record_paper_fill(_proposal(entry_cost_per_share=0.20))
        pnl = engine.settle_paper_position("market-1", "UP")

        self.assertAlmostEqual(pnl, 4.0)
        self.assertAlmostEqual(engine.state.realized_pnl, 4.0)
        self.assertEqual(engine.state.consecutive_losses, 0)
        self.assertEqual(engine.state.positions, {})


if __name__ == "__main__":
    unittest.main()
