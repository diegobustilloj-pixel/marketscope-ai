from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
CLOB_HOST = "https://clob.polymarket.com"
SENSITIVE_ENVIRONMENT_VARIABLES = (
    "PM_RFQ_API_KEY",
    "PM_RFQ_API_SECRET",
    "PM_RFQ_API_PASSPHRASE",
)
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class BootstrapError(RuntimeError):
    pass


@dataclass
class BootstrapState:
    csrf_token: str
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    exit_code: int | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _valid_address(value: object) -> bool:
    return isinstance(value, str) and ADDRESS_RE.fullmatch(value.strip()) is not None


def _network_error_message(error: BaseException) -> str:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    winerror = getattr(reason, "winerror", None)
    if isinstance(reason, PermissionError) or winerror == 10013:
        return (
            "Windows o el antivirus bloqueo la conexion HTTPS saliente de Python "
            "(WinError 10013)"
        )
    if isinstance(reason, socket.gaierror):
        return "No se pudo resolver el dominio del CLOB (DNS)"
    if isinstance(reason, ssl.SSLError):
        return "Fallo la validacion del certificado HTTPS del CLOB"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "El CLOB no respondio antes del limite de tiempo"
    if isinstance(reason, ConnectionRefusedError):
        return "La conexion con el CLOB fue rechazada"
    error_type = type(reason).__name__
    errno = getattr(reason, "errno", None)
    detail = f"tipo={error_type}"
    if errno is not None:
        detail += f", errno={errno}"
    if winerror is not None:
        detail += f", winerror={winerror}"
    return f"No se pudo conectar con el CLOB de Polymarket ({detail})"


def _check_clob_connectivity() -> dict[str, object]:
    request = urllib.request.Request(
        f"{CLOB_HOST}/time",
        headers={
            "Accept": "application/json",
            "User-Agent": "PolyMarkerQuantBot-V0.55-Connectivity-Check/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            response.read(256)
            return {
                "ok": True,
                "status": "CLOB_REACHABLE",
                "http_status": response.status,
                "credentials_requested": False,
                "signature_requested": False,
            }
    except urllib.error.HTTPError as exc:
        if exc.code in {429, 500, 502, 503, 504}:
            raise BootstrapError(
                f"El CLOB esta temporalmente no disponible (HTTP {exc.code}); intenta mas tarde"
            ) from exc
        raise BootstrapError(f"El CLOB rechazo la comprobacion publica (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BootstrapError(_network_error_message(exc)) from exc


def _parse_browser_credentials(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BootstrapError("Credenciales CLOB locales invalidas")
    field_map = {
        "api_key": "apiKey",
        "api_secret": "secret",
        "api_passphrase": "passphrase",
    }
    credentials: dict[str, str] = {}
    for destination, source in field_map.items():
        item = value.get(source)
        if not isinstance(item, str) or not item or len(item) > 4096:
            raise BootstrapError("Credenciales CLOB locales invalidas")
        credentials[destination] = item
    return credentials


def _v055_environment(
    *, credentials: Mapping[str, str], signer_address: str, maker_address: str
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PM_RFQ_API_KEY": credentials["api_key"],
            "PM_RFQ_API_SECRET": credentials["api_secret"],
            "PM_RFQ_API_PASSPHRASE": credentials["api_passphrase"],
            "PM_RFQ_SIGNER_ADDRESS": signer_address,
            "PM_RFQ_MAKER_ADDRESS": maker_address,
            "PM_RFQ_SIGNATURE_TYPE": "2",
        }
    )
    return environment


def _preflight(environment: Mapping[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "v055_monitor.py"), "--preflight"],
        cwd=ROOT,
        env=dict(environment),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError("El preflight V0.55 termino con error")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError("El preflight V0.55 devolvio una respuesta incompatible") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        missing = report.get("missing_variables", []) if isinstance(report, dict) else []
        invalid = report.get("invalid_fields", []) if isinstance(report, dict) else []
        raise BootstrapError(
            f"Preflight V0.55 bloqueado; faltantes={missing}, invalidos={invalid}"
        )
    return report


def _start_observer(
    state: BootstrapState,
    *,
    credentials: Mapping[str, str],
    signer_address: str,
    maker_address: str,
) -> None:
    environment = _v055_environment(
        credentials=credentials,
        signer_address=signer_address,
        maker_address=maker_address,
    )
    try:
        _preflight(environment)
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "v055_monitor.py"), "--run"],
            cwd=ROOT,
            env=environment,
            text=True,
        )
        with state.lock:
            state.process = process
        print("\n[V0.55] Preflight PASS. Observador iniciado por una hora.", flush=True)

        def wait_for_process() -> None:
            exit_code = process.wait()
            with state.lock:
                state.exit_code = exit_code
            print(
                f"\n[V0.55] Captura terminada con codigo {exit_code}. "
                "Ejecuta v055_monitor.py --audit una sola vez.",
                flush=True,
            )

        threading.Thread(target=wait_for_process, daemon=True).start()
    finally:
        for name in SENSITIVE_ENVIRONMENT_VARIABLES:
            environment[name] = ""


def _html(csrf_token: str) -> bytes:
    template = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PolyMarker V0.55 - Phantom</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 18px; color: #18202a; }
    label { display: block; font-weight: 650; margin: 22px 0 8px; }
    input, button { box-sizing: border-box; width: 100%; padding: 12px; font-size: 16px; }
    button { margin-top: 18px; cursor: pointer; font-weight: 700; }
    .note { background: #eef5ff; border-left: 4px solid #3478d4; padding: 12px 14px; }
    #status { white-space: pre-wrap; margin-top: 20px; font-weight: 650; }
  </style>
</head>
<body>
  <h1>V0.55 - autorización segura con Phantom</h1>
  <p class="note">Phantom pedirá firmar el mensaje <b>ClobAuth</b>. No es una transacción,
  no transfiere fondos y no autoriza órdenes. Chrome enviará esa firma directamente al CLOB
  oficial de Polymarket; las credenciales se mantienen en memoria y solo se entregan al
  observador bloqueado para órdenes.</p>
  <label for="maker">Wallet del perfil de Polymarket (Safe, 0x...)</label>
  <input id="maker" autocomplete="off" spellcheck="false" placeholder="0x...">
  <button id="start">Conectar Phantom, firmar y arrancar observador de 1 hora</button>
  <div id="status"></div>
  <script>
    const csrf = __CSRF__;
    const status = document.getElementById('status');
    const button = document.getElementById('start');
    const addressPattern = /^0x[a-fA-F0-9]{40}$/;

    function validCredentials(value) {
      return value && typeof value.apiKey === 'string' && value.apiKey.length > 0 &&
        typeof value.secret === 'string' && value.secret.length > 0 &&
        typeof value.passphrase === 'string' && value.passphrase.length > 0;
    }

    async function requestCredentials(signer, signature, timestamp) {
      const headers = {
        'Accept': 'application/json',
        'POLY_ADDRESS': signer,
        'POLY_SIGNATURE': signature,
        'POLY_TIMESTAMP': timestamp,
        'POLY_NONCE': '0'
      };
      const attempts = [
        ['crear', 'POST', 'https://clob.polymarket.com/auth/api-key'],
        ['derivar', 'GET', 'https://clob.polymarket.com/auth/derive-api-key']
      ];
      const failures = [];
      for (const [label, method, url] of attempts) {
        try {
          const response = await fetch(url, {
            method,
            headers,
            mode: 'cors',
            credentials: 'omit',
            cache: 'no-store',
            referrerPolicy: 'no-referrer'
          });
          const body = await response.text();
          let payload = {};
          try { payload = body ? JSON.parse(body) : {}; } catch (_) { payload = {}; }
          if (response.ok && validCredentials(payload)) {
            return {
              apiKey: payload.apiKey,
              secret: payload.secret,
              passphrase: payload.passphrase
            };
          }
          const remoteDetail = payload && typeof payload.error === 'string'
            ? ': ' + payload.error.slice(0, 180) : '';
          failures.push(label + ': HTTP ' + response.status + remoteDetail);
        } catch (error) {
          const detail = error && error.message ? error.message : String(error);
          failures.push(label + ': ' + detail.slice(0, 180));
        }
      }
      throw new Error('Polymarket no entregó credenciales CLOB (' + failures.join(' | ') + ').');
    }

    button.addEventListener('click', async () => {
      button.disabled = true;
      let credentials = null;
      let localPayload = null;
      status.textContent = 'Comprobando el CLOB antes de abrir Phantom...';
      try {
        const networkResponse = await fetch('/network-check', { cache: 'no-store' });
        const networkResult = await networkResponse.json();
        if (!networkResponse.ok || !networkResult.ok) {
          throw new Error(networkResult.error || 'El CLOB no está accesible.');
        }
        status.textContent = 'CLOB accesible. Esperando conexión con Phantom...';
        const provider = window.phantom && window.phantom.ethereum;
        if (!provider || !provider.isPhantom) {
          throw new Error('No se detectó la extensión Phantom EVM en este navegador.');
        }
        const maker = document.getElementById('maker').value.trim();
        if (!addressPattern.test(maker)) {
          throw new Error('La wallet del perfil debe ser una dirección 0x válida.');
        }
        try {
          await provider.request({
            method: 'wallet_switchEthereumChain',
            params: [{ chainId: '0x89' }]
          });
        } catch (_) {
          // La firma EIP-712 conserva chainId 137 aunque Phantom no cambie la vista.
        }
        const accounts = await provider.request({ method: 'eth_requestAccounts' });
        const signer = accounts && accounts[0];
        if (!addressPattern.test(signer || '')) {
          throw new Error('Phantom no devolvió una dirección EVM válida.');
        }
        if (signer.toLowerCase() === maker.toLowerCase()) {
          throw new Error('Las direcciones coinciden; este asistente está limitado a Safe tipo 2.');
        }
        const timestamp = Math.floor(Date.now() / 1000).toString();
        const typedData = {
          domain: { name: 'ClobAuthDomain', version: '1', chainId: 137 },
          types: {
            EIP712Domain: [
              { name: 'name', type: 'string' },
              { name: 'version', type: 'string' },
              { name: 'chainId', type: 'uint256' }
            ],
            ClobAuth: [
              { name: 'address', type: 'address' },
              { name: 'timestamp', type: 'string' },
              { name: 'nonce', type: 'uint256' },
              { name: 'message', type: 'string' }
            ]
          },
          primaryType: 'ClobAuth',
          message: {
            address: signer,
            timestamp,
            nonce: 0,
            message: 'This message attests that I control the given wallet'
          }
        };
        status.textContent = 'Revisa la solicitud ClobAuth en Phantom y firma si coincide.';
        const signature = await provider.request({
          method: 'eth_signTypedData_v4',
          params: [signer, JSON.stringify(typedData)]
        });
        status.textContent = 'Chrome está obteniendo las credenciales directamente desde Polymarket...';
        credentials = await requestCredentials(signer, signature, timestamp);
        status.textContent = 'Credenciales recibidas. Ejecutando preflight V0.55...';
        localPayload = JSON.stringify({ csrf, signer, maker, credentials });
        const response = await fetch('/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: localPayload
        });
        localPayload = null;
        const result = await response.json();
        if (!response.ok || !result.ok) {
          throw new Error(result.error || 'No se pudo iniciar V0.55.');
        }
        status.textContent = 'LISTO: preflight PASS y V0.55 iniciado. Mantén abierto el CMD durante una hora.';
      } catch (error) {
        status.textContent = 'BLOQUEADO: ' + (error && error.message ? error.message : String(error));
        button.disabled = false;
      } finally {
        localPayload = null;
        if (credentials) {
          credentials.apiKey = '';
          credentials.secret = '';
          credentials.passphrase = '';
          credentials = null;
        }
      }
    });
  </script>
</body>
</html>"""
    return template.replace("__CSRF__", json.dumps(csrf_token)).encode("utf-8")


def _handler(state: BootstrapState, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "PolyMarkerV055Bootstrap/2.0"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, payload: Mapping[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            route = self.path.partition("?")[0]
            if route == "/":
                body = _html(state.csrf_token)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self' https://clob.polymarket.com; img-src 'none'; "
                    "frame-ancestors 'none'; base-uri 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/status":
                with state.lock:
                    running = state.process is not None and state.process.poll() is None
                    payload = {
                        "running": running,
                        "exit_code": state.exit_code,
                        "error": state.error,
                        "real_money": "BLOQUEADO",
                    }
                self._json(HTTPStatus.OK, payload)
                return
            if route == "/network-check":
                try:
                    self._json(HTTPStatus.OK, _check_clob_connectivity())
                except BootstrapError as exc:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"ok": False, "error": str(exc), "signature_requested": False},
                    )
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Ruta no encontrada"})

        def do_POST(self) -> None:
            route = self.path.partition("?")[0]
            if route != "/start":
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Ruta no encontrada"})
                return
            origin = self.headers.get("Origin")
            if origin not in allowed_origins:
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Origen local invalido"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16_384:
                    raise BootstrapError("Solicitud local invalida")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or not secrets.compare_digest(
                    str(payload.get("csrf", "")), state.csrf_token
                ):
                    raise BootstrapError("Token local invalido")
                signer = str(payload.get("signer", "")).strip()
                maker = str(payload.get("maker", "")).strip()
                if not _valid_address(signer) or not _valid_address(maker):
                    raise BootstrapError("Direccion EVM invalida")
                if signer.lower() == maker.lower():
                    raise BootstrapError("Safe tipo 2 requiere signer y maker diferentes")
                with state.lock:
                    if state.process is not None:
                        raise BootstrapError("V0.55 ya fue iniciado desde este asistente")
                credentials = _parse_browser_credentials(payload.get("credentials"))
                try:
                    _start_observer(
                        state,
                        credentials=credentials,
                        signer_address=signer,
                        maker_address=maker,
                    )
                finally:
                    credentials.clear()
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "status": "V055_STARTED", "real_money": "BLOQUEADO"},
                )
            except BootstrapError as exc:
                with state.lock:
                    state.error = str(exc)
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception:
                with state.lock:
                    state.error = "Error local inesperado"
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": "Error local inesperado"},
                )

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap local Phantom para V0.55 Safe Wallet tipo 2"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--check-network",
        action="store_true",
        help="Comprueba el CLOB sin abrir Phantom ni solicitar una firma",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port debe estar entre 1024 y 65535")
    if args.check_network:
        try:
            payload = _check_clob_connectivity()
            exit_code = 0
        except BootstrapError as exc:
            payload = {
                "ok": False,
                "status": "BLOCKED_CLOB_CONNECTIVITY",
                "error": str(exc),
                "credentials_requested": False,
                "signature_requested": False,
                "real_money": "BLOQUEADO",
            }
            exit_code = 2
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return exit_code
    state = BootstrapState(csrf_token=secrets.token_urlsafe(32))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(state, args.port))
    print("=" * 78)
    print("V0.55 PHANTOM SAFE BOOTSTRAP - LOCAL ONLY - REAL MONEY BLOCKED")
    print("=" * 78)
    print(f"Abre en Chrome con Phantom: http://127.0.0.1:{args.port}")
    print("Transporte autenticado: Chrome -> CLOB oficial (CORS, sin archivos secretos).")
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
