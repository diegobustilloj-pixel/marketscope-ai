# Bot Truth Lab — MVP 1

Fecha de corte: 2026-08-14.

## Resultado

El MVP convierte una afirmación pública sobre un bot en un `Evidence Pack` reproducible. Cada pack contiene:

- el post exacto y una separación explícita entre hechos e interpretación;
- una especificación comprobable de entrada, cobertura, salida y ejecución;
- fuentes locales congeladas con SHA-256;
- comprobaciones que deben pasar antes de emitir el informe;
- métricas extraídas directamente de los resultados sellados;
- evidencia faltante y la siguiente prueba válida;
- barreras de seguridad que mantienen wallet, órdenes y dinero real bloqueados.

La versión 1 solo permite `FAIL` e `INSUFFICIENT_EVIDENCE`. `PASS` permanece cerrado hasta que exista un gate independiente y pre-registrado; así, el laboratorio no transforma una narrativa atractiva en una aprobación accidental.

## Casos iniciales

| Post | Hipótesis | Veredicto | Alcance |
|---|---|---|---|
| 2088025556428501021 | Inventario pareado + residual direccional | `FAIL` | Falla únicamente la interpretación mínima V0.16/V0.17; no el algoritmo exacto de nagi777. |
| 2087965513020580350 | Directional strategy + dynamic hedge | `INSUFFICIENT_EVIDENCE` | Faltan señal, switching, sizing, ejecución, wallet y prueba independiente. |
| 2032506967923515706 | Arbitraje BTC 5m con órdenes límite | `INSUFFICIENT_EVIDENCE` | Faltan reglas de cotización, ciclo de órdenes, fills, latencia, capital y wallet. |

En el caso V0.16/V0.17, la configuración `CAUTIOUS_030` registró 165 entradas, 62,42% de inventario pareado, PnL neto -30,814115 y ROI -5,49%. El filtro V0.17 alcanzó AUC 0,639277; aunque un umbral tuvo PnL total +1,415046, su segunda mitad perdió -1,635045 y no superó estabilidad temporal. Por eso no se rescató el umbral ni se abrió el holdout sellado.

## Archivos

- Motor: `src/polymarket_bot/evidence_pack.py`.
- Comando: `crear_evidence_packs_retrovalix.py`.
- Manifiestos: `data/evidence_pack_manifests/`.
- Packs terminados: `docs/evidence_packs/`.
- Pruebas: `tests/test_evidence_pack.py`.

Cada pack terminado tiene un JSON auditable y un Markdown legible. Si ambos ya existen, el comando comprueba que sus bytes sean idénticos; no los sobrescribe. Si existe solo uno o aparece un `.partial`, se detiene para auditoría manual.

## Comandos para CMD

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe crear_evidence_packs_retrovalix.py
.\.venv\Scripts\python.exe -m unittest tests.test_evidence_pack -v
```

Una ejecución correcta devuelve `CREATED` la primera vez y `VERIFIED_EXISTING` en ejecuciones posteriores.

## Compatibilidad y seguridad

- No modifica interfaces existentes del bot.
- No modifica bases de datos ni resultados sellados.
- No lee outcomes del forward activo.
- No inicia collectors, monitores ni experimentos.
- Toda prueba futura declarada por los packs queda limitada a un máximo de 24 horas.
- Dinero real y órdenes permanecen bloqueados.

## Siguiente incremento recomendado

Después de cerrar el forward principal, conectar su resultado final como un pack independiente sin reutilizar los outcomes para ajustar umbrales. Para las hipótesis RetroValix que siguen incompletas, primero debe obtenerse una wallet verificable o reglas deterministas; sin eso, el veredicto correcto continúa siendo `INSUFFICIENT_EVIDENCE`.
