from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import v056_phantom_bootstrap as base


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "data" / "v060_absolute_active_map_rfq_clob_replay.db"
PREREG = ROOT / "data" / "prereg_v060_absolute_active_map_rfq_clob_replay.json"
BootstrapError = base.BootstrapError
BootstrapState = base.BootstrapState
_parse_browser_credentials = base._parse_browser_credentials
_v060_environment = base._v056_environment
_ORIGINAL_HTML = base._html


def _preflight(environment: Mapping[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "v060_monitor.py"), "--preflight"],
        cwd=ROOT, env=dict(environment), capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError("El preflight V0.60 termino con error")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError("El preflight V0.60 devolvio una respuesta incompatible") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise BootstrapError(
            f"Preflight V0.60 bloqueado; faltantes={report.get('missing_variables', [])}, invalidos={report.get('invalid_fields', [])}"
        )
    return report


def _start_v060(
    state: BootstrapState, *, credentials: Mapping[str, str], signer_address: str, maker_address: str
) -> None:
    environment = _v060_environment(credentials=credentials, signer_address=signer_address, maker_address=maker_address)
    try:
        _preflight(environment)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "v060_monitor.py"), "--run"],
            cwd=ROOT, env=environment, text=True,
        )
        with state.lock:
            state.process = process
        print("\n[V0.60] Preflight PASS. Validando el mapa activo; la hora aun no comenzo.", flush=True)

        def announce_capture_start() -> None:
            while process.poll() is None:
                if DATABASE.is_file():
                    print(
                        "\n[V0.60] BASE CREADA: captura RFQ+CLOB iniciada. Desde ahora cuenta una hora.",
                        flush=True,
                    )
                    return
                time.sleep(0.25)

        def wait_for_process() -> None:
            exit_code = process.wait()
            with state.lock:
                state.exit_code = exit_code
            if DATABASE.is_file():
                message = "Captura terminada; revisa --status y ejecuta --audit una sola vez si es terminal."
            else:
                message = "Proceso bloqueado antes de iniciar la captura; no se creo base."
            print(f"\n[V0.60] {message} Codigo {exit_code}.", flush=True)

        threading.Thread(target=announce_capture_start, daemon=True).start()
        threading.Thread(target=wait_for_process, daemon=True).start()
    finally:
        for name in base.SENSITIVE_ENVIRONMENT_VARIABLES:
            environment[name] = ""


def _html(csrf_token: str) -> bytes:
    html = _ORIGINAL_HTML(csrf_token).decode("utf-8").replace("V0.56", "V0.60").replace("v056", "v060")
    html = html.replace(
        "V0.60 únicamente observa y mantiene el dinero real bloqueado.",
        "V0.60 valida el mapa público activo, observa RFQ y consulta libros CLOB; no envía cotizaciones ni órdenes y mantiene el dinero real bloqueado.",
    )
    watcher = r"""
    async function watchCapture() {
      for (;;) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        try {
          const response = await fetch('/status', {
            method: 'GET', cache: 'no-store', credentials: 'omit'
          });
          const current = await response.json();
          if (current.phase === 'CAPTURE_RUNNING') {
            status.textContent = 'LISTO: base creada y captura V0.60 iniciada. Mantén abierto el CMD durante una hora.';
            return;
          }
          if (current.phase === 'BLOCKED_BEFORE_CAPTURE') {
            status.textContent = 'BLOQUEADO antes de iniciar la hora. No se creó base; revisa el CMD.';
            button.disabled = false;
            return;
          }
          if (current.phase === 'CAPTURE_TERMINAL') {
            status.textContent = 'V0.60 terminó. Revisa el resultado antes de volver a ejecutar.';
            return;
          }
        } catch (_) {}
      }
    }

"""
    html = html.replace("    button.addEventListener('click', async () => {", watcher + "    button.addEventListener('click', async () => {")
    html = html.replace(
        "status.textContent = 'LISTO: preflight PASS y V0.60 iniciado. Mantén abierto el CMD durante una hora.';",
        "status.textContent = 'PREPARANDO: preflight PASS; validando mapa activo. La hora todavía no comenzó.';\n        void watchCapture();",
    )
    return html.encode("utf-8")


def _handler(state: BootstrapState, port: int) -> type:
    parent = base._handler(state, port)

    class Handler(parent):
        server_version = "PolyMarkerV060Bootstrap/1.0"

        def do_GET(self) -> None:
            route = self.path.partition("?")[0]
            if route != "/status":
                super().do_GET()
                return
            with state.lock:
                process = state.process
                running = process is not None and process.poll() is None
                exit_code = state.exit_code
                error = state.error
            database_exists = DATABASE.is_file()
            capture_status = None
            started_at_ms = None
            if database_exists:
                try:
                    from polymarket_bot.v060_audit import inspect_database

                    snapshot = inspect_database(
                        prereg_path=PREREG, database_path=DATABASE, project_root=ROOT
                    )
                    capture_status = snapshot.get("status")
                    started_at_ms = snapshot.get("run", {}).get("started_at_ms")
                except Exception:
                    capture_status = "INITIALIZING"
            if running and not database_exists:
                phase = "MAPPING_ACTIVE_UNIVERSE"
            elif running and database_exists and capture_status == "RUNNING":
                phase = "CAPTURE_RUNNING"
            elif database_exists and capture_status not in {None, "INITIALIZING", "RUNNING"}:
                phase = "CAPTURE_TERMINAL"
            elif process is not None and not running and not database_exists:
                phase = "BLOCKED_BEFORE_CAPTURE"
            elif database_exists:
                phase = "DATABASE_INITIALIZING"
            else:
                phase = "WAITING_AUTHORIZATION"
            self._json(
                HTTPStatus.OK,
                {
                    "phase": phase,
                    "running": running,
                    "exit_code": exit_code,
                    "error": error,
                    "database_exists": database_exists,
                    "capture_status": capture_status,
                    "started_at_ms": started_at_ms,
                    "orders_created": 0,
                    "paper_orders": 0,
                    "quotes_submitted": 0,
                    "transactions_created": 0,
                    "real_money": "BLOQUEADO",
                },
            )

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap local Phantom para V0.60 Safe tipo 2")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port debe estar entre 1024 y 65535")
    base._start_observer = _start_v060
    base._html = _html
    state = BootstrapState(csrf_token=secrets.token_urlsafe(32))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(state, args.port))
    print("=" * 78)
    print("V0.60 PHANTOM SAFE BOOTSTRAP - ABSOLUTE ACTIVE MAP - REAL MONEY BLOCKED")
    print("=" * 78)
    print(f"Abre en Chrome con Phantom: http://127.0.0.1:{args.port}")
    print("El navegador avisara por separado cuando la captura realmente comience.")
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
