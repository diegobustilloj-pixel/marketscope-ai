from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.v018_runner import ROOT, sha256_file


DESIGN_SCHEMA = "diagnostic_v039_validated_peer_dns_fallback_1"
VARIANT = "V0.39_VALIDATED_PEER_DNS_FALLBACK_DESIGN"
INPUTS = {
    "v038_result": "data/resultado_v038_single_layer_recovery_4h.json",
    "v038_database": "data/capture_v038_single_layer_recovery_4h.db",
    "v038_preregistration": "data/prereg_v038_single_layer_recovery_4h.json",
    "v038_implementation": "data/implementation_v038_single_layer_recovery_4h.json",
    "tls_smoke": "data/evidencia_v039_validated_peer_tls_smoke.json",
}


class V039DesignError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_v039_design(
    *, output_path: str | Path | None = None, project_root: str | Path = ROOT
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = Path(output_path).resolve() if output_path is not None else None
    hashes = {key: sha256_file(root / relative) for key, relative in INPUTS.items()}
    result = json.loads((root / INPUTS["v038_result"]).read_text(encoding="utf-8"))
    smoke = json.loads((root / INPUTS["tls_smoke"]).read_text(encoding="utf-8"))
    if result.get("verdict") != "FAIL_CLOB_SINGLE_LAYER_RECOVERY":
        raise V039DesignError("Resultado V0.38 incompatible")
    if not result.get("safety_passed"):
        raise V039DesignError("V0.38 no supero seguridad")
    failed_gates = sorted(
        key for key, passed in result["technical_gates"].items() if not bool(passed)
    )
    expected_failed_gate = "maximum_relevant_transport_recovery_ms_passed"
    if failed_gates != [expected_failed_gate]:
        raise V039DesignError("V0.38 no fallo exclusivamente por recovery SLO")

    events = list(result["transport"]["events"])
    triggers = [event for event in events if event["event_type"] == "RECONNECT_TRIGGER"]
    recoveries = {
        (str(event["condition_id"]), int(event["incident_id"])): event
        for event in events
        if event["event_type"] == "RECOVERED" and event["incident_id"] is not None
    }
    relevant_keys = {
        (str(event["condition_id"]), int(event["incident_id"]))
        for event in triggers
        if event["incident_id"] is not None and int(event["second_offset"]) <= 121
    }
    triggers_by_incident: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for event in triggers:
        if event["incident_id"] is not None:
            triggers_by_incident[
                (str(event["condition_id"]), int(event["incident_id"]))
            ].append(event)
    slow_relevant: list[dict[str, Any]] = []
    for key in sorted(relevant_keys):
        recovery = recoveries.get(key)
        if recovery is None or int(recovery["recovery_ms"] or 0) <= 10000:
            continue
        incident_triggers = triggers_by_incident[key]
        slow_relevant.append(
            {
                "condition_id": key[0],
                "slug": str(incident_triggers[0]["slug"]),
                "incident_id": key[1],
                "first_trigger_offset": min(
                    int(event["second_offset"]) for event in incident_triggers
                ),
                "last_trigger_offset": max(
                    int(event["second_offset"]) for event in incident_triggers
                ),
                "trigger_count": len(incident_triggers),
                "trigger_reasons": sorted(
                    {str(event["trigger_reason"]) for event in incident_triggers}
                ),
                "contains_dns_gaierror": any(
                    event["trigger_reason"] == "TRANSPORT_gaierror"
                    and "getaddrinfo failed" in str(event.get("transport_error") or "")
                    for event in incident_triggers
                ),
                "recovery_offset": int(recovery["second_offset"]),
                "recovery_ms": int(recovery["recovery_ms"]),
            }
        )
    gaierrors = [
        event
        for event in triggers
        if event["trigger_reason"] == "TRANSPORT_gaierror"
        and "getaddrinfo failed" in str(event.get("transport_error") or "")
    ]
    fresh_events = [event for event in events if event["event_type"] == "GENERATION_FRESH"]
    prior_fresh_for_slow = all(
        any(
            str(event["condition_id"]) == incident["condition_id"]
            and int(event["recorded_timestamp_ms"])
            < min(
                int(trigger["recorded_timestamp_ms"])
                for trigger in triggers_by_incident[
                    (incident["condition_id"], incident["incident_id"])
                ]
            )
            for event in fresh_events
        )
        for incident in slow_relevant
    )
    tls_smoke_passed = all(
        (
            smoke.get("subscription_sent") is False,
            smoke.get("normal_connection", {}).get("passed") is True,
            smoke.get("numeric_peer_connection", {}).get("passed") is True,
            smoke.get("numeric_peer_connection", {}).get(
                "original_uri_preserved"
            )
            is True,
            smoke.get("numeric_peer_connection", {}).get(
                "explicit_server_hostname_preserved"
            )
            is True,
            smoke.get("numeric_peer_connection", {}).get("tls_server_hostname")
            == smoke.get("expected_tls_server_hostname"),
            smoke.get("orders_created") == 0,
            smoke.get("real_money") == "BLOQUEADO",
        )
    )
    gates = {
        "v038_failed_only_transport_recovery_slo": failed_gates
        == [expected_failed_gate],
        "v038_exit_rate_one": result["overall"]["exit_success_within_retry_rate"]
        == 1.0,
        "v038_zero_trapped": result["overall"]["trapped_positions"] == 0,
        "v038_maximum_exit_delay_within_budget": result["overall"][
            "maximum_observed_exit_delay_seconds"
        ]
        <= 10,
        "exactly_two_slow_relevant_incidents": len(slow_relevant) == 2,
        "every_slow_relevant_incident_contains_dns_failure": bool(slow_relevant)
        and all(incident["contains_dns_gaierror"] for incident in slow_relevant),
        "slow_incidents_had_prior_tls_validated_connections": prior_fresh_for_slow,
        "numeric_peer_tls_smoke_passed": tls_smoke_passed,
        "no_parameter_grid": True,
    }
    payload = {
        "schema": DESIGN_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant": VARIANT,
        "purpose": "bypass_transient_dns_resolution_failure_with_last_tls_validated_peer",
        "inputs": {
            key: {"relative_path": relative, "sha256": hashes[key]}
            for key, relative in INPUTS.items()
        },
        "observed_failure": {
            "v038_verdict": result["verdict"],
            "failed_gates": failed_gates,
            "gaierror_getaddrinfo_events": len(gaierrors),
            "relevant_incidents": result["transport"]["relevant_incidents"],
            "unrecovered_relevant_incidents": len(
                result["transport"]["unrecovered_relevant_incidents"]
            ),
            "maximum_relevant_recovery_ms": result["transport"][
                "maximum_relevant_recovery_ms"
            ],
            "slow_relevant_incidents": slow_relevant,
            "exit_success_within_retry_rate": result["overall"][
                "exit_success_within_retry_rate"
            ],
            "trapped_positions": result["overall"]["trapped_positions"],
            "maximum_observed_exit_delay_seconds": result["overall"][
                "maximum_observed_exit_delay_seconds"
            ],
        },
        "selected_transport_remedy": {
            "reconnect_owner_count": 1,
            "cache_scope": "PROCESS_MEMORY_ONLY",
            "cache_population": "SUCCESSFUL_DIRECT_WSS_TLS_PEER_ONLY",
            "cache_peer_must_be_global_ip": True,
            "runtime_peer_ip_persisted": False,
            "normal_market_open_dial": "ORIGINAL_HOSTNAME",
            "first_incident_redial": "LAST_TLS_VALIDATED_NUMERIC_PEER_IF_AVAILABLE",
            "original_websocket_uri_preserved": True,
            "original_http_host_preserved_by_uri": True,
            "original_tls_server_hostname_explicit": True,
            "certificate_verification_disabled": False,
            "cached_peer_failure_action": "RETURN_TO_ORIGINAL_HOSTNAME",
            "cached_peer_consecutive_attempt_limit": 1,
            "fresh_generation_still_requires_both_books": True,
            "stale_depth_can_count_as_exit": False,
            "exit_probe_contract_changed": False,
            "parameter_grid_used": False,
        },
        "gates": gates,
        "all_design_gates_passed": all(gates.values()),
        "limitations": {
            "cached_peer_can_become_unreachable": True,
            "cached_peer_does_not_disable_tls_validation": True,
            "fresh_forward_validation_required": True,
            "closed_evidence_validation_credit": False,
            "economic_edge_evaluated": False,
            "orders_created": 0,
            "outcomes_read": 0,
            "prices_stored": False,
            "pnl_calculated": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        },
        "decision": "PREPARE_ONE_FRESH_V039_VALIDATED_PEER_DNS_FALLBACK_REPLICATION"
        if all(gates.values())
        else "DO_NOT_PREPARE_V039",
    }
    if output is not None:
        if output.exists():
            existing = json.loads(output.read_text(encoding="utf-8"))
            if existing == payload:
                return existing
            raise V039DesignError("Existe otro diagnostico V0.39")
        _write_atomic(output, payload)
    return payload


__all__ = ["DESIGN_SCHEMA", "V039DesignError", "run_v039_design"]
