import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.evidence_pack import (
    EvidencePackError,
    build_evidence_pack,
    write_or_verify_evidence_pack,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(source_hash: str, *, verdict: str = "FAIL") -> dict:
    return {
        "schema": "evidence_pack_manifest_1",
        "pack_id": "test_evidence_pack",
        "created_at": "2026-08-14T18:00:00Z",
        "title": "Evidence Pack de prueba",
        "verdict": verdict,
        "verdict_scope": "Solo cubre la hipótesis de prueba.",
        "source_post": {
            "id": "123456789",
            "url": "https://x.com/RetroValix/status/123456789",
            "date": "2026-08-14",
            "claim_summary": "Afirmación pública de prueba.",
        },
        "scope_statement": "No atribuye esta implementación a un tercero.",
        "interpretation_notice": "Separa hechos de interpretación.",
        "strategy_spec": {
            "market_universe": "BTC UP/DOWN 5m.",
            "decision_timing": "Un instante congelado.",
            "entry_rules": ["Regla de entrada congelada."],
            "hedge_rules": ["Regla de cobertura congelada."],
            "exit_rules": ["Mantener hasta resolución."],
            "execution_assumptions": ["Fees incluidos."],
            "unknowns": ["Prioridad de cola desconocida."],
        },
        "sources": [
            {
                "relative_path": "data/source.json",
                "sha256": source_hash,
                "role": "Fuente sellada de prueba.",
            }
        ],
        "checks": [
            {
                "label": "El gate congelado falla",
                "kind": "failure_gate",
                "source": "data/source.json",
                "field": "verdict",
                "operator": "eq",
                "expected": "FAIL_DEVELOPMENT",
            }
        ],
        "observed_metrics": [
            {
                "label": "PnL neto",
                "source": "data/source.json",
                "field": "net_pnl",
            }
        ],
        "reasons": ["El gate congelado falló."],
        "missing_evidence": ["Wallet.", "Órdenes.", "Latencia."],
        "next_valid_test": ["Congelar una prueba futura de máximo 24 horas."],
        "safety": {
            "wallet_required": False,
            "orders_enabled": False,
            "real_money": "BLOQUEADO",
            "new_experiment_opened": False,
            "active_forward_read": False,
            "active_forward_modified": False,
            "maximum_new_experiment_hours": 24,
        },
    }


class EvidencePackTests(unittest.TestCase):
    def _fixture(self, directory: str, *, verdict: str = "FAIL"):
        root = Path(directory)
        data = root / "data"
        data.mkdir()
        source = data / "source.json"
        source.write_text(
            json.dumps({"verdict": "FAIL_DEVELOPMENT", "net_pnl": -2.5}),
            encoding="utf-8",
        )
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(_manifest(_sha256(source), verdict=verdict)),
            encoding="utf-8",
        )
        return root, source, manifest

    def test_build_is_deterministic_and_proves_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, manifest = self._fixture(directory)
            first_pack, first_markdown = build_evidence_pack(manifest, root=root)
            second_pack, second_markdown = build_evidence_pack(manifest, root=root)

        self.assertEqual(first_pack, second_pack)
        self.assertEqual(first_markdown, second_markdown)
        self.assertEqual(first_pack["verdict"], "FAIL")
        self.assertTrue(first_pack["evidence_checks"][0]["passed"])
        self.assertIn("Dinero real: bloqueado", first_markdown)

    def test_tampered_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest = self._fixture(directory)
            source.write_text('{"verdict":"PASS","net_pnl":99}', encoding="utf-8")

            with self.assertRaisesRegex(EvidencePackError, "Hash de fuente"):
                build_evidence_pack(manifest, root=root)

    def test_pass_is_fail_closed_in_version_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, manifest = self._fixture(directory, verdict="PASS")

            with self.assertRaisesRegex(EvidencePackError, "PASS no esta habilitado"):
                build_evidence_pack(manifest, root=root)

    def test_insufficient_evidence_requires_three_specific_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, manifest = self._fixture(
                directory, verdict="INSUFFICIENT_EVIDENCE"
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["missing_evidence"] = ["Solo una carencia."]
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(EvidencePackError, "tres carencias"):
                build_evidence_pack(manifest, root=root)

    def test_safety_cannot_enable_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, manifest = self._fixture(directory)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["safety"]["orders_enabled"] = True
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(EvidencePackError, "orders_enabled"):
                build_evidence_pack(manifest, root=root)

    def test_write_is_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, manifest = self._fixture(directory)
            output = root / "packs"
            created = write_or_verify_evidence_pack(manifest, output, root=root)
            verified = write_or_verify_evidence_pack(manifest, output, root=root)

        self.assertEqual(created["status"], "CREATED")
        self.assertEqual(verified["status"], "VERIFIED_EXISTING")

    def test_real_retrovalix_manifests_match_frozen_sources(self):
        project = Path(__file__).resolve().parents[1]
        manifests = sorted(
            (project / "data" / "evidence_pack_manifests").glob("*.json")
        )
        verdicts = [
            build_evidence_pack(path, root=project)[0]["verdict"]
            for path in manifests
        ]

        self.assertEqual(len(manifests), 4)
        self.assertEqual(verdicts.count("FAIL"), 1)
        self.assertEqual(verdicts.count("INSUFFICIENT_EVIDENCE"), 3)


if __name__ == "__main__":
    unittest.main()
