from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file
from polymarket_bot.v038_contract import (
    aggregate_probe_results,
    evaluate_market_probes,
    evaluate_probe,
)


PREREG_SCHEMA = "prereg_v039_validated_peer_dns_fallback_4h_1"
PREREG_STATUS = (
    "FROZEN_VALIDATED_PEER_DNS_FALLBACK_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL"
)
VARIANT = "V0.39_FRESH_VALIDATED_PEER_DNS_FALLBACK_4H"
PASS_VERDICT = "PASS_FRESH_VALIDATED_PEER_DNS_FALLBACK_EXIT_SAFETY_ONLY"


class V039ContractError(RuntimeError):
    pass


def _require(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise V039ContractError(f"V0.39 incompatible: {field}")


def load_and_verify_prereg(
    path: str | Path, *, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path).resolve()
    if not source.is_file():
        raise V039ContractError("Preinscripcion V0.39 no encontrada")
    payload = json.loads(source.read_text(encoding="utf-8"))
    _require(payload.get("schema"), PREREG_SCHEMA, "schema")
    _require(payload.get("status"), PREREG_STATUS, "status")
    _require(payload.get("variant"), VARIANT, "variant")
    _require(
        payload.get("scope", {}).get("maximum_positive_result"),
        PASS_VERDICT,
        "scope.maximum_positive_result",
    )
    _require(
        payload.get("transport_contract"),
        {
            "enabled": True,
            "scope": "BOTH_CLOB_BOOKS",
            "reconnect_owner_count": 1,
            "socket_session_has_internal_reconnect": False,
            "supervisor_owns_connect_receive_staleness_and_reconnect": True,
            "age_source": "OLDEST_UP_OR_DOWN_CLOB_SOURCE_TIMESTAMP",
            "stale_timeout_ms": 3000,
            "poll_ms": 250,
            "open_timeout_ms": 3000,
            "close_timeout_ms": 250,
            "heartbeat_ms": 10000,
            "reconnect_backoff_schedule_ms": [250, 500, 1000],
            "reconnect_backoff_cap_ms": 1000,
            "recovery_service_level_ms": 10000,
            "normal_market_open_dial": "ORIGINAL_HOSTNAME",
            "first_incident_redial": "LAST_TLS_VALIDATED_NUMERIC_PEER_IF_AVAILABLE",
            "validated_peer_cache_scope": "PROCESS_MEMORY_ONLY",
            "validated_peer_cache_population": "SUCCESSFUL_DIRECT_WSS_TLS_PEER_ONLY",
            "validated_peer_must_be_global_ip": True,
            "validated_peer_fallback_disabled_when_proxy_enabled": True,
            "original_websocket_uri_preserved": True,
            "original_http_host_preserved_by_uri": True,
            "original_tls_server_hostname_explicit": True,
            "certificate_verification_disabled": False,
            "runtime_peer_ip_persisted": False,
            "cached_peer_consecutive_attempt_limit": 1,
            "cached_peer_failure_action": "RETURN_TO_ORIGINAL_HOSTNAME",
            "record_dial_mode_cache_availability_and_recovery": True,
            "connected_label_alone_is_sufficient": False,
            "fresh_generation_requires_both_books_updated_after_generation_start": True,
            "stale_depth_can_count_as_exit": False,
            "unrecovered_or_slow_relevant_incident_action": "FAIL_CLOSED",
        },
        "transport_contract",
    )
    previous = json.loads(
        (
            root
            / payload["evidence"]["v038_preregistration"]["relative_path"]
        ).read_text(encoding="utf-8")
    )
    _require(payload.get("probe_contract"), previous.get("probe_contract"), "probe_contract")
    _require(payload.get("capture_contract"), previous.get("capture_contract"), "capture_contract")
    _require(payload.get("stopping", {}).get("maximum_hours"), 4.0, "stopping.maximum_hours")
    _require(payload.get("stopping", {}).get("scheduled_supervision"), False, "stopping.scheduled_supervision")
    _require(payload.get("stopping", {}).get("automatic_final_audit"), True, "stopping.automatic_final_audit")
    _require(payload.get("duration_rationale", {}).get("expected_capacity_probes"), 5856, "duration.expected_capacity_probes")
    _require(payload.get("duration_rationale", {}).get("fresh_fallback_exercise_required_for_pass"), True, "duration.fallback_exercise")
    _require(payload.get("technical_gates", {}).get("minimum_validated_peer_fallback_fresh_recoveries"), 1, "gates.fallback_recoveries")
    _require(payload.get("data_policy", {}).get("outcomes_read"), 0, "data_policy.outcomes_read")
    _require(payload.get("data_policy", {}).get("prices_stored"), False, "data_policy.prices_stored")
    _require(payload.get("data_policy", {}).get("pnl_calculated"), False, "data_policy.pnl_calculated")
    _require(payload.get("data_policy", {}).get("runtime_peer_ip_stored"), False, "data_policy.runtime_peer_ip_stored")
    _require(payload.get("safety", {}).get("orders_enabled"), False, "safety.orders_enabled")
    _require(payload.get("safety", {}).get("paper_orders_enabled"), False, "safety.paper_orders_enabled")
    _require(payload.get("safety", {}).get("wallet_required"), False, "safety.wallet_required")
    _require(payload.get("safety", {}).get("real_money"), "BLOQUEADO", "safety.real_money")
    for key, record in payload.get("evidence", {}).items():
        relative = str(record.get("relative_path") or "")
        _require(record.get("sha256"), sha256_file(root / relative), f"evidence.{key}.sha256")
    design = payload.get("design_code", {})
    _require(
        design.get("sha256"),
        sha256_file(root / str(design.get("relative_path"))),
        "design_code.sha256",
    )
    diagnostic = json.loads(
        (
            root
            / payload["evidence"]["validated_peer_design_diagnostic"][
                "relative_path"
            ]
        ).read_text(encoding="utf-8")
    )
    _require(
        diagnostic.get("decision"),
        "PREPARE_ONE_FRESH_V039_VALIDATED_PEER_DNS_FALLBACK_REPLICATION",
        "diagnostic.decision",
    )
    _require(diagnostic.get("all_design_gates_passed"), True, "diagnostic.gates")
    remedy = diagnostic.get("selected_transport_remedy", {})
    _require(remedy.get("reconnect_owner_count"), 1, "diagnostic.reconnect_owner")
    _require(remedy.get("certificate_verification_disabled"), False, "diagnostic.tls")
    _require(remedy.get("runtime_peer_ip_persisted"), False, "diagnostic.peer_persistence")
    return dict(payload)


__all__ = [
    "PASS_VERDICT",
    "VARIANT",
    "V039ContractError",
    "aggregate_probe_results",
    "evaluate_market_probes",
    "evaluate_probe",
    "load_and_verify_prereg",
]
