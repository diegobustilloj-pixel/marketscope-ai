from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any


BOT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ALLOWED_STAGES = {
    "draft",
    "research",
    "backtest",
    "shadow",
    "paper",
    "approved",
    "live",
    "paused",
    "archived",
}
ALLOWED_EXECUTION_MODES = {
    "read_only",
    "manual_signal",
    "shadow",
    "paper",
    "live",
}


class ManifestError(ValueError):
    """Raised when a bot manifest violates the registry contract."""


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: JSON inválido: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{path}: el documento debe ser un objeto JSON")
    return payload


def discover_manifests(project_root: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    root = (project_root or default_project_root()).resolve()
    manifest_dir = root / "configs" / "bots"
    if not manifest_dir.is_dir():
        raise ManifestError(f"No existe el catálogo: {manifest_dir}")
    results: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(manifest_dir.glob("*.json")):
        if path.name == "registry.json":
            continue
        results.append((path, _load_json(path)))
    return results


def validate_manifest(
    manifest: dict[str, Any],
    path: Path,
    project_root: Path,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    required = {
        "schema_version",
        "bot_id",
        "display_name",
        "domain",
        "version",
        "lifecycle_stage",
        "enabled",
        "managed_by_registry",
        "execution",
        "entrypoint",
        "data_dir",
        "safety",
    }
    missing = sorted(required - set(manifest))
    if missing:
        errors.append(f"faltan campos: {', '.join(missing)}")

    bot_id = manifest.get("bot_id")
    if not isinstance(bot_id, str) or not BOT_ID_PATTERN.fullmatch(bot_id):
        errors.append("bot_id debe usar kebab-case: letras minúsculas, números y guiones")
    elif path.stem != bot_id:
        errors.append(f"el archivo debe llamarse {bot_id}.json")

    stage = manifest.get("lifecycle_stage")
    if stage not in ALLOWED_STAGES:
        errors.append(f"lifecycle_stage inválido: {stage!r}")

    execution = manifest.get("execution")
    if not isinstance(execution, dict):
        errors.append("execution debe ser un objeto")
    elif execution.get("mode") not in ALLOWED_EXECUTION_MODES:
        errors.append(f"execution.mode inválido: {execution.get('mode')!r}")

    for flag in ("enabled", "managed_by_registry"):
        if not isinstance(manifest.get(flag), bool):
            errors.append(f"{flag} debe ser booleano")

    safety = manifest.get("safety")
    if not isinstance(safety, dict):
        errors.append("safety debe ser un objeto")
    else:
        for flag in ("real_money", "automatic_orders", "withdrawals"):
            if safety.get(flag) is not False:
                errors.append(f"safety.{flag} debe permanecer false en esta plataforma")

    if stage == "live" or (isinstance(execution, dict) and execution.get("mode") == "live"):
        errors.append("live está bloqueado: requiere un proceso de aprobación que aún no existe")

    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, dict) or not isinstance(entrypoint.get("script"), str):
        errors.append("entrypoint.script es obligatorio")
    else:
        script_path = (project_root / entrypoint["script"]).resolve()
        try:
            script_path.relative_to(project_root.resolve())
        except ValueError:
            errors.append("entrypoint.script no puede salir del proyecto")
        else:
            if not script_path.is_file():
                errors.append(f"entrypoint inexistente: {entrypoint['script']}")

    data_dir = manifest.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir:
        errors.append("data_dir debe ser una ruta relativa")
    else:
        resolved_data = (project_root / data_dir).resolve()
        try:
            resolved_data.relative_to(project_root.resolve())
        except ValueError:
            errors.append("data_dir no puede salir del proyecto")
        else:
            if not resolved_data.exists():
                warnings.append(f"data_dir todavía no existe: {data_dir}")

    return errors, warnings


def validate_registry(project_root: Path | None = None) -> dict[str, Any]:
    root = (project_root or default_project_root()).resolve()
    records = discover_manifests(root)
    seen: set[str] = set()
    errors: list[str] = []
    warnings: list[str] = []
    for path, manifest in records:
        bot_id = manifest.get("bot_id")
        if isinstance(bot_id, str) and bot_id in seen:
            errors.append(f"{path.name}: bot_id duplicado: {bot_id}")
        if isinstance(bot_id, str):
            seen.add(bot_id)
        manifest_errors, manifest_warnings = validate_manifest(manifest, path, root)
        errors.extend(f"{path.name}: {message}" for message in manifest_errors)
        warnings.extend(f"{path.name}: {message}" for message in manifest_warnings)
    return {
        "status": "OK" if not errors else "INVALID",
        "bots": len(records),
        "errors": errors,
        "warnings": warnings,
        "project_root": str(root),
    }


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def create_draft_bot(
    project_root: Path,
    bot_id: str,
    display_name: str,
    domain: str,
) -> dict[str, Any]:
    root = project_root.resolve()
    if not BOT_ID_PATTERN.fullmatch(bot_id):
        raise ManifestError("bot_id inválido; ejemplo válido: sports-nba-v1")
    config_path = root / "configs" / "bots" / f"{bot_id}.json"
    app_dir = root / "apps" / bot_id
    main_path = app_dir / "main.py"
    readme_path = app_dir / "README.md"
    occupied = [path for path in (config_path, app_dir) if path.exists()]
    if occupied:
        raise ManifestError(f"el bot ya existe: {', '.join(str(path) for path in occupied)}")

    app_dir.mkdir(parents=True, exist_ok=False)
    main_path.write_text(
        "from __future__ import annotations\n\n"
        "import json\n\n\n"
        "def main() -> int:\n"
        f"    payload = {{\"bot_id\": \"{bot_id}\", \"status\": \"DRAFT_BLOCKED\"}}\n"
        "    print(json.dumps(payload, ensure_ascii=False, indent=2))\n"
        "    return 0\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
        newline="\n",
    )
    readme_path.write_text(
        f"# {display_name}\n\n"
        "Bot creado desde la plantilla segura. Está bloqueado en `draft`, no usa dinero real "
        "y no envía órdenes. Defina hipótesis, datos, backtest y límites antes de cambiar su etapa.\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schema_version": "1.0.0",
        "bot_id": bot_id,
        "display_name": display_name,
        "domain": domain,
        "version": "0.1.0",
        "lifecycle_stage": "draft",
        "enabled": False,
        "managed_by_registry": False,
        "execution": {"mode": "read_only", "environment": "development"},
        "entrypoint": {"script": f"apps/{bot_id}/main.py", "module": None},
        "data_dir": f"data/{bot_id}",
        "safety": {"real_money": False, "automatic_orders": False, "withdrawals": False},
        "tags": ["generated", "draft"],
        "notes": "Creado por la fábrica segura; requiere preregistro y pruebas.",
    }
    try:
        _atomic_json_write(config_path, manifest)
    except BaseException:
        main_path.unlink(missing_ok=True)
        readme_path.unlink(missing_ok=True)
        app_dir.rmdir()
        raise
    return manifest


def _print_table(records: list[tuple[Path, dict[str, Any]]]) -> None:
    headers = ("BOT ID", "DOMINIO", "ETAPA", "MODO", "GESTIONADO", "DINERO REAL")
    rows = []
    for _, item in records:
        rows.append((
            str(item.get("bot_id", "?")),
            str(item.get("domain", "?")),
            str(item.get("lifecycle_stage", "?")),
            str(item.get("execution", {}).get("mode", "?")),
            "sí" if item.get("managed_by_registry") else "no",
            "sí" if item.get("safety", {}).get("real_money") else "no",
        ))
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Catálogo seguro de bots de ProyectoBotV4")
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list", help="Listar todos los bots declarados")
    list_parser.add_argument("--json", action="store_true", dest="as_json")
    commands.add_parser("validate", help="Validar catálogo, rutas y bloqueos de seguridad")
    show_parser = commands.add_parser("show", help="Mostrar el manifiesto de un bot")
    show_parser.add_argument("bot_id")
    create_parser = commands.add_parser("create", help="Crear un bot nuevo, seguro y bloqueado")
    create_parser.add_argument("bot_id")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--domain", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.project_root.resolve()
    try:
        if args.command == "validate":
            result = validate_registry(root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "OK" else 2
        if args.command == "list":
            records = discover_manifests(root)
            if args.as_json:
                print(json.dumps([manifest for _, manifest in records], ensure_ascii=False, indent=2))
            else:
                _print_table(records)
            return 0
        if args.command == "show":
            matches = [item for _, item in discover_manifests(root) if item.get("bot_id") == args.bot_id]
            if not matches:
                raise ManifestError(f"bot desconocido: {args.bot_id}")
            print(json.dumps(matches[0], ensure_ascii=False, indent=2))
            return 0
        manifest = create_draft_bot(root, args.bot_id, args.name, args.domain)
        print(json.dumps({"status": "CREATED_DRAFT", "manifest": manifest}, ensure_ascii=False, indent=2))
        return 0
    except ManifestError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
