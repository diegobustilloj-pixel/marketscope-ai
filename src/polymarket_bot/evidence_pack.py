from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCHEMA = "evidence_pack_manifest_1"
PACK_SCHEMA = "evidence_pack_1"
ALLOWED_VERDICTS = {"FAIL", "INSUFFICIENT_EVIDENCE"}
ALLOWED_CHECK_KINDS = {"identity", "failure_gate", "safety"}
ALLOWED_OPERATORS = {"eq", "contains", "lt", "lte", "gt", "gte", "is_null"}
PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class EvidencePackError(RuntimeError):
    """Raised when an evidence pack cannot be proven from its frozen sources."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidencePackError(f"JSON no legible: {path}") from exc


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvidencePackError(f"Campo de texto obligatorio ausente: {key}")
    return value.strip()


def _require_text_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise EvidencePackError(f"Lista obligatoria vacia: {key}")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise EvidencePackError(f"Elemento invalido en {key}")
        items.append(item.strip())
    return items


def _safe_source(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute():
        raise EvidencePackError("Las fuentes deben usar rutas relativas")
    root = root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise EvidencePackError(f"Fuente fuera del proyecto: {relative_path}")
    if not candidate.is_file():
        raise EvidencePackError(f"Fuente inexistente: {relative_path}")
    return candidate


def _resolve_record(payload: Any, match: dict[str, Any] | None) -> Any:
    if match is None:
        return payload
    if not isinstance(payload, list):
        raise EvidencePackError("record_match solo es valido sobre una lista JSON")
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and all(item.get(str(key)) == value for key, value in match.items())
    ]
    if len(matches) != 1:
        raise EvidencePackError(
            f"record_match debe resolver un registro unico; encontro {len(matches)}"
        )
    return matches[0]


def _resolve_field(payload: Any, field: str) -> Any:
    current = payload
    if not field:
        return current
    for part in field.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        raise EvidencePackError(f"Campo no encontrado: {field}")
    return current


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "contains":
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, (list, tuple, set, dict)):
            return expected in actual
        return False
    if operator == "is_null":
        return actual is None
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError) as exc:
        raise EvidencePackError(
            f"Comparacion numerica invalida: {actual!r} {operator} {expected!r}"
        ) from exc
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    raise EvidencePackError(f"Operador no soportado: {operator}")


def _validate_safety(manifest: dict[str, Any]) -> dict[str, Any]:
    safety = manifest.get("safety")
    if not isinstance(safety, dict):
        raise EvidencePackError("Bloque safety ausente")
    expected = {
        "wallet_required": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
        "new_experiment_opened": False,
        "active_forward_read": False,
        "active_forward_modified": False,
        "maximum_new_experiment_hours": 24,
    }
    for key, value in expected.items():
        if safety.get(key) != value:
            raise EvidencePackError(f"Safety fail-closed incumplido: {key}")
    return dict(expected)


def _validate_strategy_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    spec = manifest.get("strategy_spec")
    if not isinstance(spec, dict):
        raise EvidencePackError("strategy_spec ausente")
    return {
        "market_universe": _require_text(spec, "market_universe"),
        "decision_timing": _require_text(spec, "decision_timing"),
        "entry_rules": _require_text_list(spec, "entry_rules"),
        "hedge_rules": _require_text_list(spec, "hedge_rules"),
        "exit_rules": _require_text_list(spec, "exit_rules"),
        "execution_assumptions": _require_text_list(
            spec, "execution_assumptions"
        ),
        "unknowns": _require_text_list(spec, "unknowns"),
    }


def _load_sources(
    manifest: dict[str, Any], root: Path
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EvidencePackError("Debe existir al menos una fuente congelada")
    loaded: dict[str, Any] = {}
    frozen: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise EvidencePackError("Fuente invalida")
        relative = _require_text(source, "relative_path").replace("\\", "/")
        expected_hash = _require_text(source, "sha256").lower()
        role = _require_text(source, "role")
        if relative in loaded:
            raise EvidencePackError(f"Fuente duplicada: {relative}")
        path = _safe_source(root, relative)
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise EvidencePackError(f"Hash de fuente no coincide: {relative}")
        loaded[relative] = _load_json(path)
        frozen.append(
            {"relative_path": relative, "sha256": actual_hash, "role": role}
        )
    return loaded, frozen


def _run_checks(
    manifest: dict[str, Any], sources: dict[str, Any]
) -> list[dict[str, Any]]:
    checks = manifest.get("checks")
    if not isinstance(checks, list) or not checks:
        raise EvidencePackError("El pack requiere comprobaciones ejecutables")
    results: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            raise EvidencePackError("Comprobacion invalida")
        source = _require_text(check, "source").replace("\\", "/")
        if source not in sources:
            raise EvidencePackError(f"Comprobacion usa fuente no congelada: {source}")
        kind = _require_text(check, "kind")
        if kind not in ALLOWED_CHECK_KINDS:
            raise EvidencePackError(f"Tipo de comprobacion no permitido: {kind}")
        operator = _require_text(check, "operator")
        if operator not in ALLOWED_OPERATORS:
            raise EvidencePackError(f"Operador no permitido: {operator}")
        record_match = check.get("record_match")
        if record_match is not None and not isinstance(record_match, dict):
            raise EvidencePackError("record_match debe ser un objeto")
        record = _resolve_record(sources[source], record_match)
        field = str(check.get("field") or "")
        actual = _resolve_field(record, field)
        expected = check.get("expected")
        passed = _compare(actual, operator, expected)
        if not passed:
            raise EvidencePackError(
                f"Evidencia no confirma '{_require_text(check, 'label')}': "
                f"actual={actual!r} {operator} expected={expected!r}"
            )
        results.append(
            {
                "label": _require_text(check, "label"),
                "kind": kind,
                "source": source,
                "field": field,
                "operator": operator,
                "expected": expected,
                "actual": actual,
                "passed": True,
            }
        )
    return results


def _observed_metrics(
    manifest: dict[str, Any], sources: dict[str, Any]
) -> list[dict[str, Any]]:
    metrics = manifest.get("observed_metrics", [])
    if not isinstance(metrics, list):
        raise EvidencePackError("observed_metrics debe ser una lista")
    output: list[dict[str, Any]] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            raise EvidencePackError("Metrica invalida")
        source = _require_text(metric, "source").replace("\\", "/")
        if source not in sources:
            raise EvidencePackError(f"Metrica usa fuente no congelada: {source}")
        record_match = metric.get("record_match")
        if record_match is not None and not isinstance(record_match, dict):
            raise EvidencePackError("record_match de metrica invalido")
        record = _resolve_record(sources[source], record_match)
        field = _require_text(metric, "field")
        output.append(
            {
                "label": _require_text(metric, "label"),
                "source": source,
                "field": field,
                "value": _resolve_field(record, field),
                "format": str(metric.get("format") or "plain"),
            }
        )
    return output


def build_evidence_pack(
    manifest_path: str | Path, *, root: str | Path = ROOT
) -> tuple[dict[str, Any], str]:
    root_path = Path(root).resolve()
    manifest_file = Path(manifest_path).resolve()
    manifest = _load_json(manifest_file)
    if not isinstance(manifest, dict):
        raise EvidencePackError("Manifest incompatible")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise EvidencePackError("Schema de manifest incompatible")
    pack_id = _require_text(manifest, "pack_id")
    if PACK_ID_RE.fullmatch(pack_id) is None:
        raise EvidencePackError("pack_id invalido")
    verdict = _require_text(manifest, "verdict")
    if verdict == "PASS":
        raise EvidencePackError(
            "PASS no esta habilitado en Evidence Pack v1 sin un gate independiente"
        )
    if verdict not in ALLOWED_VERDICTS:
        raise EvidencePackError(f"Veredicto no permitido: {verdict}")
    source_post = manifest.get("source_post")
    if not isinstance(source_post, dict):
        raise EvidencePackError("source_post ausente")
    post_id = _require_text(source_post, "id")
    expected_url = f"https://x.com/RetroValix/status/{post_id}"
    if _require_text(source_post, "url") != expected_url:
        raise EvidencePackError("URL de source_post no coincide con su id")

    sources, frozen_sources = _load_sources(manifest, root_path)
    checks = _run_checks(manifest, sources)
    if verdict == "FAIL" and not any(
        item["kind"] == "failure_gate" for item in checks
    ):
        raise EvidencePackError("FAIL requiere al menos un failure_gate comprobado")
    missing = manifest.get("missing_evidence", [])
    if not isinstance(missing, list) or any(
        not isinstance(item, str) or not item.strip() for item in missing
    ):
        raise EvidencePackError("missing_evidence invalido")
    if verdict == "INSUFFICIENT_EVIDENCE" and len(missing) < 3:
        raise EvidencePackError(
            "INSUFFICIENT_EVIDENCE requiere al menos tres carencias concretas"
        )

    strategy_spec = _validate_strategy_spec(manifest)
    safety = _validate_safety(manifest)
    reasons = _require_text_list(manifest, "reasons")
    next_test = _require_text_list(manifest, "next_valid_test")
    payload = {
        "schema": PACK_SCHEMA,
        "pack_id": pack_id,
        "created_at": _require_text(manifest, "created_at"),
        "title": _require_text(manifest, "title"),
        "verdict": verdict,
        "verdict_scope": _require_text(manifest, "verdict_scope"),
        "source_post": {
            "id": post_id,
            "url": expected_url,
            "date": _require_text(source_post, "date"),
            "claim_summary": _require_text(source_post, "claim_summary"),
        },
        "scope_statement": _require_text(manifest, "scope_statement"),
        "interpretation_notice": _require_text(manifest, "interpretation_notice"),
        "strategy_spec": strategy_spec,
        "frozen_sources": frozen_sources,
        "evidence_checks": checks,
        "observed_metrics": _observed_metrics(manifest, sources),
        "reasons": reasons,
        "missing_evidence": [str(item).strip() for item in missing],
        "next_valid_test": next_test,
        "safety": safety,
        "manifest_sha256": sha256_file(manifest_file),
    }
    return payload, render_markdown(payload)


def _markdown_cell(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        text = f"{value:.8f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _metric_value(metric: dict[str, Any]) -> str:
    value = metric["value"]
    if metric.get("format") == "percent" and isinstance(value, (int, float)):
        return f"{100.0 * float(value):.2f}%"
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def render_markdown(pack: dict[str, Any]) -> str:
    spec = pack["strategy_spec"]
    post = pack["source_post"]
    lines = [
        f"# {pack['title']}",
        "",
        f"**Veredicto:** `{pack['verdict']}`",
        "",
        f"**Alcance del veredicto:** {pack['verdict_scope']}",
        "",
        f"**Post fuente:** [{post['id']}]({post['url']}) — {post['date']}",
        "",
        "## Afirmación pública resumida",
        "",
        post["claim_summary"],
        "",
        "## Alcance y distinción obligatoria",
        "",
        pack["scope_statement"],
        "",
        f"**Interpretación:** {pack['interpretation_notice']}",
        "",
        "## Especificación comprobable",
        "",
        f"- **Universo:** {spec['market_universe']}",
        f"- **Momento de decisión:** {spec['decision_timing']}",
        "- **Entradas:**",
    ]
    lines.extend(f"  - {item}" for item in spec["entry_rules"])
    lines.append("- **Cobertura:**")
    lines.extend(f"  - {item}" for item in spec["hedge_rules"])
    lines.append("- **Salidas:**")
    lines.extend(f"  - {item}" for item in spec["exit_rules"])
    lines.append("- **Supuestos de ejecución:**")
    lines.extend(f"  - {item}" for item in spec["execution_assumptions"])
    lines.extend(["", "## Desconocidos", ""])
    lines.extend(f"- {item}" for item in spec["unknowns"])
    lines.extend(
        [
            "",
            "## Comprobaciones ejecutables",
            "",
            "| Comprobación | Tipo | Fuente | Campo | Resultado |",
            "|---|---|---|---|---|",
        ]
    )
    for check in pack["evidence_checks"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(check["label"]),
                    _markdown_cell(check["kind"]),
                    _markdown_cell(check["source"]),
                    _markdown_cell(check["field"]),
                    f"PASS ({_markdown_cell(check['actual'])})",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Métricas observadas", ""])
    if pack["observed_metrics"]:
        lines.extend(["| Métrica | Valor | Fuente |", "|---|---:|---|"])
        for metric in pack["observed_metrics"]:
            lines.append(
                f"| {_markdown_cell(metric['label'])} | "
                f"{_markdown_cell(_metric_value(metric))} | "
                f"{_markdown_cell(metric['source'])} |"
            )
    else:
        lines.append("No hay métricas de rentabilidad atribuibles a esta estrategia.")
    lines.extend(["", "## Razones del veredicto", ""])
    lines.extend(f"- {item}" for item in pack["reasons"])
    lines.extend(["", "## Evidencia que falta", ""])
    if pack["missing_evidence"]:
        lines.extend(f"- {item}" for item in pack["missing_evidence"])
    else:
        lines.append("- No se declaró evidencia faltante adicional.")
    lines.extend(["", "## Siguiente prueba válida", ""])
    lines.extend(f"- {item}" for item in pack["next_valid_test"])
    lines.extend(
        [
            "",
            "## Seguridad",
            "",
            "- Wallet: no requerida.",
            "- Órdenes: bloqueadas.",
            "- Dinero real: bloqueado.",
            "- Forward activo: no leído y no modificado.",
            "- Experimento nuevo: no abierto.",
            "- Duración máxima futura: 24 horas.",
            "",
            "## Provenance",
            "",
            f"- Manifest SHA-256: `{pack['manifest_sha256']}`",
        ]
    )
    for source in pack["frozen_sources"]:
        lines.append(
            f"- `{source['relative_path']}` — `{source['sha256']}` — {source['role']}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _encoded_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def write_or_verify_evidence_pack(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    root: str | Path = ROOT,
) -> dict[str, Any]:
    payload, markdown = build_evidence_pack(manifest_path, root=root)
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{payload['pack_id']}.json"
    markdown_path = directory / f"{payload['pack_id']}.md"
    expected_json = _encoded_json(payload)
    expected_markdown = markdown.encode("utf-8")
    existing = (json_path.exists(), markdown_path.exists())
    if existing == (True, True):
        if json_path.read_bytes() != expected_json:
            raise EvidencePackError(f"Pack JSON existente no coincide: {json_path}")
        if markdown_path.read_bytes() != expected_markdown:
            raise EvidencePackError(
                f"Pack Markdown existente no coincide: {markdown_path}"
            )
        return {
            "pack_id": payload["pack_id"],
            "status": "VERIFIED_EXISTING",
            "json": str(json_path),
            "markdown": str(markdown_path),
        }
    if existing != (False, False):
        raise EvidencePackError("Pack incompleto: existe solo uno de sus archivos")
    partial_json = json_path.with_suffix(json_path.suffix + ".partial")
    partial_markdown = markdown_path.with_suffix(markdown_path.suffix + ".partial")
    if partial_json.exists() or partial_markdown.exists():
        raise EvidencePackError("Existe un pack parcial; requiere auditoria manual")
    _write_exclusive(partial_json, expected_json)
    try:
        _write_exclusive(partial_markdown, expected_markdown)
    except Exception:
        raise
    partial_json.replace(json_path)
    partial_markdown.replace(markdown_path)
    return {
        "pack_id": payload["pack_id"],
        "status": "CREATED",
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


__all__ = [
    "EvidencePackError",
    "build_evidence_pack",
    "render_markdown",
    "sha256_file",
    "write_or_verify_evidence_pack",
]
