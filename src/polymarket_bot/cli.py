from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence as ArgSequence
from pathlib import Path

from polymarket_bot.app import (
    audit_database,
    configure_logging,
    database_diagnostics,
    database_status,
    discover_once,
    fast_verify_database,
    run_collect,
    run_demo,
    settings_with_db,
    verify_database,
)
from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError
from polymarket_bot.legacy_export import export_v3_dataset
from polymarket_bot.phase2 import build_silver_dataset, silver_status
from polymarket_bot.phase3 import build_gold_dataset, gold_status
from polymarket_bot.phase4 import build_phase4_models, phase4_status
from polymarket_bot.phase41 import (
    audit_shadow_forward,
    prepare_shadow_models,
    run_shadow_forward,
    shadow_status,
)
from polymarket_bot.paper_risk import paper_risk_status
from polymarket_bot.runtime_policy import (
    BacktestRuntimeExceeded,
    backtest_runtime_guard,
    enforce_forward_duration,
)
from polymarket_bot.system_guard import prevent_system_sleep


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-bot",
        description=(
            "Fases 1-4.2: datos, modelos y shadow, sin IA ni órdenes"
        ),
    )
    parser.add_argument(
        "--db",
        help=(
            "Ruta SQLite alternativa "
            "(predeterminado: data/polymarket_phase1_v4.db)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("demo", help="Demo local sin internet ni dinero")

    discover = subparsers.add_parser(
        "discover", help="Descubrir el mercado BTC 5m"
    )
    discover.add_argument("--slug", help="Slug exacto opcional")

    collect = subparsers.add_parser(
        "collect", help="Recolectar Binance, RTDS y CLOB públicos"
    )
    collect.add_argument(
        "--seconds",
        type=float,
        default=0,
        help="Duración; 0 mantiene el proceso hasta Ctrl+C",
    )
    collect.add_argument("--slug", help="Slug exacto opcional")
    collect.add_argument(
        "--max-db-gb",
        type=float,
        default=30.0,
        help="Parada segura al alcanzar este tamaño; 0 desactiva",
    )
    collect.add_argument(
        "--min-free-gb",
        type=float,
        default=40.0,
        help="Parada segura al llegar a este espacio libre; 0 desactiva",
    )
    collect.add_argument(
        "--keep-awake",
        action="store_true",
        help="Evitar temporalmente la suspensión automática de Windows",
    )

    subparsers.add_parser("status", help="Resumen de la base")
    subparsers.add_parser(
        "diagnose",
        help="Medir tasa, compresión y almacenamiento proyectado",
    )
    subparsers.add_parser("verify", help="Verificar checksums")
    subparsers.add_parser(
        "verify-fast",
        help="Verificación rápida para datasets grandes",
    )
    audit = subparsers.add_parser(
        "audit",
        help="Evaluar el gate de calidad de la captura",
    )
    audit.add_argument("--min-hours", type=float, default=71.5)
    audit.add_argument("--min-coverage", type=float, default=0.995)
    export_v3 = subparsers.add_parser(
        "export-v3",
        help="Exportar resumen y muestras pequeñas de una base V3",
    )
    export_v3.add_argument(
        "--source-db",
        required=True,
        help="Ruta de la base V3 original (se abre en modo de solo lectura)",
    )
    export_v3.add_argument(
        "--output",
        default="export_72h_muestra.zip",
        help="ZIP pequeño de salida",
    )
    export_v3.add_argument(
        "--samples-per-stream",
        type=int,
        default=5,
        help="Máximo de ejemplos por fuente/stream",
    )
    silver = subparsers.add_parser(
        "build-silver",
        help="Crear dataset normalizado por segundo desde una captura V3/V4",
    )
    silver.add_argument(
        "--source-db",
        required=True,
        help="Base raw V3 o V4; se abre exclusivamente en modo de lectura",
    )
    silver.add_argument(
        "--output-db",
        required=True,
        help="Nueva base Silver de salida",
    )
    silver.add_argument(
        "--max-markets",
        type=int,
        help="Limitar la prueba a los primeros N mercados",
    )
    silver.add_argument(
        "--offline-labels",
        action="store_true",
        help="No consultar las resoluciones públicas de Gamma",
    )
    silver.add_argument(
        "--label-cache",
        help=(
            "Caché JSON reanudable de resoluciones Gamma verificadas"
        ),
    )
    silver.add_argument(
        "--report-file",
        help="Guardar también el resultado JSON en este archivo",
    )
    silver.add_argument(
        "--keep-awake",
        action="store_true",
        help="Evitar temporalmente la suspensión automática de Windows",
    )
    silver_status_parser = subparsers.add_parser(
        "silver-status",
        help="Verificar y resumir una base Silver",
    )
    silver_status_parser.add_argument(
        "--silver-db",
        required=True,
        help="Ruta de la base Silver",
    )
    gold = subparsers.add_parser(
        "build-gold",
        help="Crear features Gold y división temporal desde Silver",
    )
    gold.add_argument(
        "--v3-silver-db",
        required=True,
        help="Base Silver completa derivada de V3",
    )
    gold.add_argument(
        "--v4-silver-db",
        required=True,
        help="Base Silver completa derivada de V4",
    )
    gold.add_argument(
        "--output-db",
        required=True,
        help="Nueva base Gold de salida",
    )
    gold.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=[15, 30, 60, 90],
        help="Segundos restantes en cada punto de decisión",
    )
    gold.add_argument(
        "--report-file",
        help="Guardar también el resultado JSON en este archivo",
    )
    gold_status_parser = subparsers.add_parser(
        "gold-status",
        help="Verificar y resumir una base Gold",
    )
    gold_status_parser.add_argument(
        "--gold-db",
        required=True,
        help="Ruta de la base Gold",
    )
    phase4 = subparsers.add_parser(
        "build-models",
        help="Comparar modelos, calibrar y hacer backtest sin órdenes",
    )
    phase4.add_argument(
        "--gold-db",
        required=True,
        help="Base Gold v2 aprobada; se abre solo en lectura",
    )
    phase4.add_argument(
        "--output-db",
        required=True,
        help="Nueva base de resultados Fase 4",
    )
    phase4.add_argument(
        "--model-file",
        required=True,
        help="Artefacto congelado si validación selecciona estrategia",
    )
    phase4.add_argument(
        "--fee-rate",
        type=float,
        default=0.07,
        help="Parámetro de comisión taker de mercados cripto",
    )
    phase4.add_argument(
        "--slippage-per-share",
        type=float,
        default=0.005,
        help="Deslizamiento adverso supuesto por participación",
    )
    phase4.add_argument(
        "--report-file",
        help="Guardar también el resultado JSON en este archivo",
    )
    phase4.add_argument(
        "--max-runtime-hours",
        type=float,
        default=24.0,
        help="Limite obligatorio de ejecucion; nunca puede superar 24 horas",
    )
    phase4_status_parser = subparsers.add_parser(
        "phase4-status",
        help="Verificar resultados y gate estadístico Fase 4",
    )
    phase4_status_parser.add_argument(
        "--phase4-db",
        required=True,
        help="Ruta de la base de resultados Fase 4",
    )
    prepare_shadow = subparsers.add_parser(
        "prepare-shadow",
        help="Congelar hipótesis Fase 4.1 sin abrir test",
    )
    prepare_shadow.add_argument("--gold-db", required=True)
    prepare_shadow.add_argument("--phase4-db", required=True)
    prepare_shadow.add_argument("--output-model", required=True)
    prepare_shadow.add_argument("--report-file")
    run_shadow = subparsers.add_parser(
        "run-shadow",
        help="Ejecutar forward shadow ligero y reanudable",
    )
    run_shadow.add_argument("--model-file", required=True)
    run_shadow.add_argument("--output-db", required=True)
    run_shadow.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help=(
            "Duracion maxima de experimentos nuevos: 24h. La base forward "
            "de siete dias ya iniciada puede reanudarse por excepcion congelada."
        ),
    )
    run_shadow.add_argument("--max-db-gb", type=float, default=1.0)
    run_shadow.add_argument("--min-free-gb", type=float, default=20.0)
    run_shadow.add_argument(
        "--keep-awake",
        action="store_true",
        help="Evitar temporalmente la suspensión automática de Windows",
    )
    shadow_status_parser = subparsers.add_parser(
        "shadow-status",
        help="Consultar progreso de forward shadow",
    )
    shadow_status_parser.add_argument("--shadow-db", required=True)
    audit_shadow = subparsers.add_parser(
        "audit-shadow",
        help="Auditar forward shadow y gates estadísticos",
    )
    audit_shadow.add_argument("--shadow-db", required=True)
    audit_shadow.add_argument("--phase4-db", required=True)
    audit_shadow.add_argument("--report-file")
    paper_risk = subparsers.add_parser(
        "paper-risk-status",
        help="Auditar perfil de riesgo paper-only sin activar ordenes",
    )
    paper_risk.add_argument(
        "--profile",
        default="data/paper_risk_profile_draft.json",
        help="Perfil JSON de riesgo paper",
    )
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv: ArgSequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    base_settings = Settings.from_env()
    settings = settings_with_db(base_settings, args.db)
    configure_logging(base_settings.log_level)
    try:
        if args.command == "demo":
            _print(asyncio.run(run_demo(settings)))
        elif args.command == "discover":
            _print(asyncio.run(discover_once(settings, args.slug)))
        elif args.command == "collect":
            with prevent_system_sleep(args.keep_awake):
                result = asyncio.run(
                    run_collect(
                        settings,
                        seconds=args.seconds,
                        preferred_slug=args.slug,
                        max_db_gb=args.max_db_gb,
                        min_free_gb=args.min_free_gb,
                    )
                )
            _print(result)
            if result["safety"]["stop_reason"] is not None:
                raise SystemExit(4)
        elif args.command == "status":
            _print(database_status(settings))
        elif args.command == "diagnose":
            _print(database_diagnostics(settings))
        elif args.command == "verify":
            result = verify_database(settings)
            _print(result)
            if not result["ok"]:
                raise SystemExit(2)
        elif args.command == "verify-fast":
            result = fast_verify_database(settings)
            _print(result)
            if not result["ok"]:
                raise SystemExit(2)
        elif args.command == "audit":
            result = audit_database(
                settings,
                min_hours=args.min_hours,
                min_coverage=args.min_coverage,
            )
            _print(result)
            if not result["passed"]:
                raise SystemExit(5)
        elif args.command == "export-v3":
            _print(
                export_v3_dataset(
                    source_db=args.source_db,
                    output_zip=args.output,
                    samples_per_stream=args.samples_per_stream,
                )
            )
        elif args.command == "build-silver":
            with prevent_system_sleep(args.keep_awake):
                result = build_silver_dataset(
                    source_db=args.source_db,
                    output_db=args.output_db,
                    max_markets=args.max_markets,
                    fetch_labels=not args.offline_labels,
                    label_cache=args.label_cache,
                )
            _print(result)
            if args.report_file:
                report_path = Path(args.report_file)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(
                        result,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if not result["passed"]:
                raise SystemExit(7)
        elif args.command == "silver-status":
            _print(silver_status(args.silver_db))
        elif args.command == "build-gold":
            result = build_gold_dataset(
                v3_silver_db=args.v3_silver_db,
                v4_silver_db=args.v4_silver_db,
                output_db=args.output_db,
                horizons=args.horizons,
            )
            _print(result)
            if args.report_file:
                report_path = Path(args.report_file)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(
                        result,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if not result["passed"]:
                raise SystemExit(8)
        elif args.command == "gold-status":
            _print(gold_status(args.gold_db))
        elif args.command == "build-models":
            with backtest_runtime_guard(args.max_runtime_hours):
                result = build_phase4_models(
                    gold_db=args.gold_db,
                    output_db=args.output_db,
                    model_file=args.model_file,
                    fee_rate=args.fee_rate,
                    slippage_per_share=args.slippage_per_share,
                )
            _print(result)
            if args.report_file:
                report_path = Path(args.report_file)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(
                        result,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if not result["pipeline_passed"]:
                raise SystemExit(9)
        elif args.command == "phase4-status":
            _print(phase4_status(args.phase4_db))
        elif args.command == "prepare-shadow":
            result = prepare_shadow_models(
                gold_db=args.gold_db,
                phase4_db=args.phase4_db,
                output_model=args.output_model,
            )
            _print(result)
            if args.report_file:
                report_path = Path(args.report_file)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(
                        result,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        elif args.command == "run-shadow":
            enforce_forward_duration(args.hours, args.output_db)
            with prevent_system_sleep(args.keep_awake):
                result = asyncio.run(
                    run_shadow_forward(
                        settings=settings,
                        model_file=args.model_file,
                        output_db=args.output_db,
                        target_hours=args.hours,
                        max_database_gb=args.max_db_gb,
                        min_free_gb=args.min_free_gb,
                    )
            )
            _print(result)
            if result["status"] == "SAFETY_STOP":
                raise SystemExit(10)
            if result["status"] in {"FAILED", "INTERRUPTED"}:
                raise SystemExit(11)
        elif args.command == "shadow-status":
            _print(shadow_status(args.shadow_db))
        elif args.command == "audit-shadow":
            result = audit_shadow_forward(
                shadow_db=args.shadow_db,
                phase4_db=args.phase4_db,
            )
            _print(result)
            if args.report_file:
                report_path = Path(args.report_file)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(
                        result,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if not result["experiment_complete"]:
                raise SystemExit(11)
            if not result["technical_passed"]:
                raise SystemExit(12)
        elif args.command == "paper-risk-status":
            _print(paper_risk_status(args.profile))
    except DiscoveryError as exc:
        print(f"ERROR DE DISCOVERY: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    except ValueError as exc:
        print(f"ERROR DE CONFIGURACION: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except BacktestRuntimeExceeded as exc:
        print(f"LIMITE DE BACKTEST: {exc}", file=sys.stderr)
        raise SystemExit(124) from exc
    except KeyboardInterrupt:
        print("\nRecolector detenido de forma segura.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
