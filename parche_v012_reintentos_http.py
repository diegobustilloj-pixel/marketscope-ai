
from pathlib import Path
import shutil

p = Path("execution_collector_v012.py")
if not p.exists():
    raise SystemExit("ERROR: no encuentro execution_collector_v012.py en esta carpeta")

text = p.read_text(encoding="utf-8")
backup = Path("execution_collector_v012_pre_reintentos_http.bak")

if "import urllib.error" not in text:
    text = text.replace(
        "import urllib.parse\nimport urllib.request\n",
        "import urllib.error\nimport urllib.parse\nimport urllib.request\n",
        1,
    )

old_func = '''def http_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
'''

new_func = '''def http_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_json_retry(
    url: str,
    timeout: float,
    attempts: int = 3,
    retry_delay_seconds: float = 0.12,
) -> Any:
    retryable_http = {429, 500, 502, 503, 504}
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return http_json(url, min(float(timeout), 2.0))
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_http:
                raise
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc

        if attempt < attempts:
            print(
                f"[{utc_now()}] RETRY HTTP {attempt}/{attempts - 1} "
                f"para {url}"
            )
            time.sleep(retry_delay_seconds * attempt)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("http_json_retry fallo sin excepcion")
'''

if "def http_json_retry(" not in text:
    if old_func not in text:
        raise SystemExit("ERROR: no encontre la funcion http_json esperada")
    text = text.replace(old_func, new_func, 1)

pairs = [
    ("info_raw = http_json(info_url, timeout)", "info_raw = http_json_retry(info_url, timeout)"),
    ("up_raw = http_json(up_url, timeout)", "up_raw = http_json_retry(up_url, timeout)"),
    ("down_raw = http_json(down_url, timeout)", "down_raw = http_json_retry(down_url, timeout)"),
]

for old, new in pairs:
    if new in text:
        continue
    if old not in text:
        raise SystemExit(f"ERROR: no encontre el bloque esperado: {old}")
    text = text.replace(old, new, 1)

if not backup.exists():
    shutil.copy2(p, backup)

compile(text, str(p), "exec")
p.write_text(text, encoding="utf-8")

print("PATCH RETRY OK")
print("Backup:", backup)
print("Reintentos: hasta 3 por llamada")
print("HTTP reintentables: 429, 500, 502, 503, 504")
print("URLError/DNS: reintentable")
print("Timeout maximo por intento: 2.0 s")
print("No se modificaron senales, thresholds, labels, PnL ni preregistros")
