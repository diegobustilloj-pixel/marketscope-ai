from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v051_prefunded_maker_census import evaluate_market, normalize_standard_market
from polymarket_bot.v051b_contract import (
    build_preregistration,
    frozen_contract,
    load_and_verify_preregistration,
)
from polymarket_bot.v051b_prefunded_maker_census import (
    census_v051b,
    validate_corrected_standard_clob_market,
)
from tests.test_v051_prefunded_maker_census import (
    _clob_info,
    _raw_event,
    _sources,
    _validated,
)


PREREG = ROOT / "data" / "prereg_v051b_prefunded_maker_transport_correction_census.json"
RESULT = ROOT / "data" / "resultado_v051b_prefunded_maker_transport_correction_census.json"


class V051bPrefundedMakerCensusTests(unittest.TestCase):
    def test_correction_accepts_only_absent_or_false_clob_flag(self) -> None:
        event = _raw_event(1)
        market, reason = normalize_standard_market(
            event, event["markets"][0], frozen_contract()  # type: ignore[index]
        )
        self.assertIsNone(reason)
        assert market is not None
        missing = _clob_info(1)
        del missing["neg_risk"]
        validated, why = validate_corrected_standard_clob_market(market, missing, 5)
        self.assertIsNone(why)
        assert validated is not None
        self.assertEqual(validated["clob_neg_risk_field_state"], "ABSENT")
        explicit_false, why = validate_corrected_standard_clob_market(
            market, _clob_info(1), 5
        )
        self.assertIsNone(why)
        assert explicit_false is not None
        self.assertEqual(explicit_false["clob_neg_risk_field_state"], "FALSE")
        true_info = _clob_info(1)
        true_info["neg_risk"] = True
        self.assertEqual(
            validate_corrected_standard_clob_market(market, true_info, 5)[1],
            "CLOB_NEG_RISK_TRUE",
        )

    def test_rest_token_flag_remains_fail_closed(self) -> None:
        market = _validated(1)
        metadata, books = _sources(
            market,
            yes_ask=0.60,
            yes_bid=0.55,
            no_ask=0.45,
            no_bid=0.40,
        )
        metadata[str(market["yes_token_id"])]["neg_risk"] = None
        self.assertEqual(
            evaluate_market(market, metadata, books, frozen_contract())[1],
            "YES_REST_METADATA_NEG_RISK_NOT_EXPLICITLY_FALSE",
        )

    def test_offline_corrected_census_reaches_economic_rejection(self) -> None:
        gamma_events = [_raw_event(index) for index in range(1, 16)]
        market_map = {
            str(event["markets"][0]["conditionId"]): event["markets"][0]  # type: ignore[index]
            for event in gamma_events
        }
        token_map = {
            token: market
            for market in market_map.values()
            for token in json.loads(str(market["clobTokenIds"]))
        }

        def fake_http(method: str, url: str, body: object | None) -> object:
            if "gamma-api" in url:
                return gamma_events
            if "/clob-markets/" in url:
                condition = url.rsplit("/", 1)[-1]
                info = _clob_info(int(market_map[condition]["id"]))
                del info["neg_risk"]
                return info
            if url.endswith("/books"):
                response = []
                for item in body:  # type: ignore[union-attr]
                    token = item["token_id"]
                    raw = token_map[token]
                    response.append(
                        {
                            "asset_id": token,
                            "market": raw["conditionId"],
                            "timestamp": "1000",
                            "hash": f"rest-{token}",
                            "min_order_size": "5",
                            "tick_size": "0.01",
                            "neg_risk": False,
                        }
                    )
                return response
            raise AssertionError(url)

        async def fake_ws(endpoint: str, tokens: object, contract: object) -> dict[str, object]:
            books = {}
            for index in range(1, 16):
                market = _validated(index)
                _, market_books = _sources(
                    market,
                    yes_ask=0.60,
                    yes_bid=0.55,
                    no_ask=0.45,
                    no_bid=0.40,
                )
                books.update(market_books)
            now_ms = time.time_ns() // 1_000_000
            return {
                "connected_at_ms": now_ms,
                "finished_at_ms": now_ms,
                "elapsed_seconds": 0.001,
                "expected_tokens": len(tokens),  # type: ignore[arg-type]
                "received_tokens": len(tokens),  # type: ignore[arg-type]
                "missing_tokens": [],
                "malformed_messages": 0,
                "books": books,
            }

        with tempfile.TemporaryDirectory() as temporary:
            prereg_path = Path(temporary) / "prereg.json"
            result_path = Path(temporary) / "result.json"
            build_preregistration(output_path=prereg_path, project_root=ROOT)
            result = census_v051b(
                prereg_path=prereg_path,
                result_path=result_path,
                project_root=ROOT,
                http_json=fake_http,
                ws_collector=fake_ws,
            )
        self.assertEqual(result["transport_correction"]["clob_neg_risk_field_states"], {"ABSENT": 15})
        self.assertEqual(result["census"]["markets_synchronized"], 15)
        self.assertEqual(result["census"]["directions_screened"], 30)
        self.assertEqual(result["census"]["cost_candidates_before_gas"], 0)
        self.assertEqual(result["verdict"], "REJECT_PREFUNDED_MAKER_SINGLE_FILL_HEDGE_FEASIBILITY")

    def test_official_artifacts_preserve_safety(self) -> None:
        if not PREREG.is_file():
            self.skipTest("Se congela antes del censo oficial corregido")
        prereg = load_and_verify_preregistration(PREREG, project_root=ROOT)
        self.assertFalse(prereg["safety"]["orders_enabled"])
        self.assertFalse(prereg["safety"]["transactions_enabled"])
        if RESULT.is_file():
            result = json.loads(RESULT.read_text(encoding="utf-8"))
            self.assertEqual(result["safety"]["orders_created"], 0)
            self.assertEqual(result["safety"]["transactions_created"], 0)
            self.assertEqual(result["safety"]["real_money"], "BLOQUEADO")


if __name__ == "__main__":
    unittest.main()
