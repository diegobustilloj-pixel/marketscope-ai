from pathlib import Path
import shutil

p = Path("execution_collector_v012.py")
if not p.exists():
    raise SystemExit("ERROR: no encuentro execution_collector_v012.py en esta carpeta")

text = p.read_text(encoding="utf-8")
backup = Path("execution_collector_v012_pre_tick_dinamico.bak")

old = (
'            if book_tick is not None and abs(float(book_tick) - info["tick_size"]) > 1e-12:\n'
'                raise RuntimeError(f"{side}: tick del book no coincide con market info")\n'
)

new = (
'            if book_tick is not None and abs(float(book_tick) - info["tick_size"]) > 1e-12:\n'
'                # Polymarket puede cambiar dinamicamente el tick (p.ej. 0.01 -> 0.001)\n'
'                # cuando el precio entra en zonas extremas. El /book es el snapshot\n'
'                # contemporaneo, asi que una diferencia de tick NO invalida el book.\n'
'                print(\n'
'                    f"[{utc_now()}] INFO {side}: tick dinamico "\n'
'                    f"book={book_tick} market_info={info[\'tick_size\']}"\n'
'                )\n'
)

old_store = (
'                info["tick_size"],\n'
'                info["min_order_size"],\n'
)

new_store = (
'                min(\n'
'                    float(up_raw.get("tick_size") or info["tick_size"]),\n'
'                    float(down_raw.get("tick_size") or info["tick_size"]),\n'
'                ),\n'
'                info["min_order_size"],\n'
)

if old not in text:
    if "tick dinamico" in text:
        print("PATCH YA APLICADO")
        raise SystemExit(0)
    raise SystemExit("ERROR: no encontre el bloque esperado de validacion de tick")

if old_store not in text:
    raise SystemExit("ERROR: no encontre el bloque esperado donde se guarda tick_size")

if not backup.exists():
    shutil.copy2(p, backup)

text = text.replace(old, new, 1)
text = text.replace(old_store, new_store, 1)

compile(text, str(p), "exec")
p.write_text(text, encoding="utf-8")

print("PATCH OK")
print("Backup:", backup)
print("Cambio: tick dinamico ya no produce FAILED")
print("El tick guardado usa el tick actual observado en /book")
print("No se modificaron thresholds, labels, PnL ni reglas del pipeline")
