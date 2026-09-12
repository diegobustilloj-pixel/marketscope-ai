from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def classify_root_file(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in {".zip", ".bak"}:
        return "archive"
    if suffix in {".bat", ".ps1"}:
        return "windows_operations"
    if suffix in {".md", ".txt"}:
        return "documentation_or_report"
    if suffix == ".py":
        if any(token in name for token in ("monitor", "shadow", "collector", "paper", "sentinel")):
            return "runtime_entrypoint"
        if any(token in name for token in ("audit", "analiz", "evalu", "report", "postmortem")):
            return "analysis_entrypoint"
        return "python_entrypoint_or_tool"
    if name.startswith(".env") or name in {"pyproject.toml", ".gitignore", ".gitattributes"}:
        return "project_configuration"
    return "unclassified"


def root_file_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((item for item in root.iterdir() if item.is_file()), key=lambda item: item.name.lower()):
        stat = path.stat()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "category": classify_root_file(path),
                "extension": path.suffix.lower() or "(none)",
                "size_bytes": stat.st_size,
                "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return rows


def data_rows(root: Path) -> list[dict[str, Any]]:
    data_root = root / "data"
    rows: list[dict[str, Any]] = []
    if not data_root.is_dir():
        return rows
    root_files = [item for item in data_root.iterdir() if item.is_file() and item.name != "README.md"]
    if root_files:
        root_stats = []
        for path in root_files:
            try:
                root_stats.append(path.stat())
            except OSError:
                continue
        if root_stats:
            newest_mtime = max(stat.st_mtime for stat in root_stats)
            size_bytes = sum(stat.st_size for stat in root_stats)
            rows.append(
                {
                    "data_dir": "data/(root files)",
                    "file_count": len(root_stats),
                    "size_bytes": size_bytes,
                    "size_gib": round(size_bytes / (1024**3), 3),
                    "newest_modified_utc": datetime.fromtimestamp(
                        newest_mtime, tz=timezone.utc
                    ).isoformat(),
                }
            )
    for directory in sorted((item for item in data_root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
        file_count = 0
        size_bytes = 0
        newest_mtime = 0.0
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            size_bytes += stat.st_size
            newest_mtime = max(newest_mtime, stat.st_mtime)
        rows.append(
            {
                "data_dir": directory.relative_to(root).as_posix(),
                "file_count": file_count,
                "size_bytes": size_bytes,
                "size_gib": round(size_bytes / (1024**3), 3),
                "newest_modified_utc": (
                    datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()
                    if newest_mtime
                    else ""
                ),
            }
        )
    return rows


def bot_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    manifest_dir = root / "configs" / "bots"
    if not manifest_dir.is_dir():
        return rows
    for path in sorted(manifest_dir.glob("*.json")):
        if path.name == "registry.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        execution = payload.get("execution") or {}
        safety = payload.get("safety") or {}
        entrypoint = payload.get("entrypoint") or {}
        rows.append(
            {
                "bot_id": payload.get("bot_id", ""),
                "display_name": payload.get("display_name", ""),
                "domain": payload.get("domain", ""),
                "lifecycle_stage": payload.get("lifecycle_stage", ""),
                "execution_mode": execution.get("mode", ""),
                "enabled": bool(payload.get("enabled", False)),
                "managed_by_registry": bool(payload.get("managed_by_registry", False)),
                "real_money": bool(safety.get("real_money", False)),
                "entrypoint": entrypoint.get("script", ""),
                "data_dir": payload.get("data_dir", ""),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    path: Path,
    root_files: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    bots: list[dict[str, Any]],
) -> None:
    generated = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    categories = Counter(row["category"] for row in root_files)
    total_data_bytes = sum(row["size_bytes"] for row in datasets)
    lines = [
        "# Catálogo del proyecto",
        "",
        "> Generado automáticamente. No editar a mano; ejecutar "
        "`.venv\\Scripts\\python.exe tools\\build_project_catalog.py`.",
        "",
        f"Actualizado (UTC): `{generated}`",
        "",
        "## Resumen",
        "",
        f"- Bots registrados: **{len(bots)}**",
        f"- Bots habilitados: **{sum(1 for row in bots if row['enabled'])}**",
        f"- Bots con dinero real: **{sum(1 for row in bots if row['real_money'])}**",
        f"- Archivos sueltos en la raíz: **{len(root_files)}**",
        f"- Directorios de datos: **{len(datasets)}**",
        f"- Volumen de datos local: **{total_data_bytes / (1024**3):.2f} GiB**",
        "",
        "## Bots registrados",
        "",
        "| Bot | Dominio | Etapa | Modo | Habilitado | Datos |",
        "|---|---|---|---|---:|---|",
    ]
    for row in bots:
        lines.append(
            f"| `{row['bot_id']}` | {row['domain']} | {row['lifecycle_stage']} | "
            f"{row['execution_mode']} | {'sí' if row['enabled'] else 'no'} | `{row['data_dir']}` |"
        )
    lines.extend(
        [
            "",
            "## Datos locales",
            "",
            "| Directorio | Archivos | Tamaño (GiB) |",
            "|---|---:|---:|",
        ]
    )
    for row in sorted(datasets, key=lambda item: item["size_bytes"], reverse=True):
        lines.append(f"| `{row['data_dir']}` | {row['file_count']} | {row['size_gib']:.3f} |")
    lines.extend(
        [
            "",
            "## Archivos de raíz por categoría",
            "",
            "| Categoría | Cantidad |",
            "|---|---:|",
        ]
    )
    for category, count in sorted(categories.items()):
        lines.append(f"| {category} | {count} |")
    lines.extend(
        [
            "",
            "Los inventarios detallados están en `artifacts/inventory/`. Los datos y resultados "
            "pesados permanecen fuera de Git por diseño.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_catalog(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    inventory_dir = root / "artifacts" / "inventory"
    roots = root_file_rows(root)
    datasets = data_rows(root)
    bots = bot_rows(root)
    write_csv(
        inventory_dir / "root_file_catalog.csv",
        roots,
        ["path", "category", "extension", "size_bytes", "modified_utc"],
    )
    write_csv(
        inventory_dir / "data_catalog.csv",
        datasets,
        ["data_dir", "file_count", "size_bytes", "size_gib", "newest_modified_utc"],
    )
    write_csv(
        inventory_dir / "bot_catalog.csv",
        bots,
        [
            "bot_id",
            "display_name",
            "domain",
            "lifecycle_stage",
            "execution_mode",
            "enabled",
            "managed_by_registry",
            "real_money",
            "entrypoint",
            "data_dir",
        ],
    )
    write_markdown(root / "docs" / "PROJECT_CATALOG.md", roots, datasets, bots)
    return {
        "status": "OK",
        "bots": len(bots),
        "root_files": len(roots),
        "data_directories": len(datasets),
        "data_bytes": sum(row["size_bytes"] for row in datasets),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generar catálogo reproducible de ProyectoBotV4")
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    args = parser.parse_args()
    print(json.dumps(build_catalog(args.project_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
