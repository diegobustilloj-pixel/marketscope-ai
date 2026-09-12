from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import v056_phantom_bootstrap as base


ROOT = Path(__file__).resolve().parent
BootstrapError = base.BootstrapError
BootstrapState = base.BootstrapState
_parse_browser_credentials = base._parse_browser_credentials
_valid_address = base._valid_address
_v058_environment = base._v056_environment
_ORIGINAL_HTML = base._html


def _preflight(environment: Mapping[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "v058_monitor.py"), "--preflight"],
        cwd=ROOT, env=dict(environment), capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError("El preflight V0.58 termino con error")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError("El preflight V0.58 devolvio una respuesta incompatible") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise BootstrapError(
            f"Preflight V0.58 bloqueado; faltantes={report.get('missing_variables', [])}, invalidos={report.get('invalid_fields', [])}"
        )
    return report


def _start_v058(
    state: BootstrapState, *, credentials: Mapping[str, str], signer_address: str, maker_address: str
) -> None:
    environment = _v058_environment(credentials=credentials, signer_address=signer_address, maker_address=maker_address)
    try:
        _preflight(environment)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "v058_monitor.py"), "--run"],
            cwd=ROOT, env=environment, text=True,
        )
        with state.lock:
            state.process = process
        print("\n[V0.58] Preflight PASS. Construyendo mapa position-to-token antes de conectar RFQ.", flush=True)

        def wait_for_process() -> None:
            exit_code = process.wait()
            with state.lock:
                state.exit_code = exit_code
            print(
                f"\n[V0.58] Captura terminada con codigo {exit_code}. Ejecuta v058_monitor.py --audit una sola vez.",
                flush=True,
            )

        threading.Thread(target=wait_for_process, daemon=True).start()
    finally:
        for name in base.SENSITIVE_ENVIRONMENT_VARIABLES:
            environment[name] = ""


def _html(csrf_token: str) -> bytes:
    html = _ORIGINAL_HTML(csrf_token).decode("utf-8").replace("V0.56", "V0.58").replace("v056", "v058")
    html = html.replace(
        "V0.58 únicamente observa y mantiene el dinero real bloqueado.",
        "V0.58 precarga el mapa público position-to-token, observa RFQ y consulta libros CLOB; no envía cotizaciones ni órdenes y mantiene el dinero real bloqueado.",
    )
    return html.encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap local Phantom para V0.58 Safe tipo 2")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port debe estar entre 1024 y 65535")
    base._start_observer = _start_v058
    base._html = _html
    state = BootstrapState(csrf_token=secrets.token_urlsafe(32))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), base._handler(state, args.port))
    print("=" * 78)
    print("V0.58 PHANTOM SAFE BOOTSTRAP - MAPPED RFQ+CLOB - REAL MONEY BLOCKED")
    print("=" * 78)
    print(f"Abre en Chrome con Phantom: http://127.0.0.1:{args.port}")
    print("Solo auth RFQ y datos publicos; cero quotes y cero ordenes.")
    print("No cierres este CMD. Ctrl+C detiene el asistente local.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
