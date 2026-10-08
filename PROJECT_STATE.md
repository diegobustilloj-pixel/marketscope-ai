# Estado canónico — Plataforma cuantitativa Polymarket

**Corte de contexto:** 7 de octubre de 2026 (America/La_Paz)
**Raíz canónica:** `C:\ProyectoBotV4\polymarket_quant_bot`
**Regla de seguridad vigente:** no hay firma, conexión de wallet, retiros, dinero real ni órdenes automáticas habilitados.

Este documento es la fuente breve de continuidad para una persona o agente que retome el proyecto. Debe leerse junto a `CONTINUE_PROJECT.md`, no sustituye los artefactos ni permite afirmar resultados que no estén respaldados por evidencia local sellada.

## Estado ejecutivo

La plataforma concentra investigaciones de Polymarket bajo un núcleo compartido. V2 y V3 son antecedentes de captura; V4 es la única fuente de código activa. Los datos pesados se preservan localmente y quedan fuera de Git.

| Línea | Estado verificable | Decisión actual |
|---|---|---|
| PolyLedger P0 | Replay reconciliado; dos lotes dejaron 3/9 candidatos aceptados sólo para un bundle futuro, pero cero marcas están integradas | Continuar desde el cursor 40, completar flujos y segundo cálculo; sin trading |
| Deportes / wallets | Investigación y prueba cerradas; no surgió un ganador copiable | No construir ejecutor ni usar capital |
| Clima | Investigación, backtest realista y ticket manual existen | Mantener manual/shadow hasta nueva evidencia |
| Elon / conteo de tuits | Investigación y monitor forward separado existen | No olvidar esta línea; revisar su evidencia local antes de informar resultados |
| BTC 5m | Forward y gates históricos; candidatos recientes no aprobaron | Mantener bloqueadas órdenes y dinero real |
| Market making / rewards | Investigación shadow | No interpretar recompensas configuradas como ganancia realizada |

Los estados permitidos son `draft → research → backtest → shadow → paper → approved → live`. En este repositorio, `live` permanece bloqueado por diseño.

## Qué se versiona y qué no

| Se publica en Git | Se conserva localmente y no se publica |
|---|---|
| Código, pruebas, configuraciones, esquemas, documentación y artefactos pequeños sin secretos | `data/`, `logs/`, bases SQLite/DuckDB, capturas raw, CSV/Parquet masivos, ZIP, modelos, archivos `.partial`, credenciales y claves |

La política completa está en `docs/operations/DATA_POLICY.md`. `.gitignore` es una barrera de publicación, **no** un respaldo: los datos irremplazables deben conservarse también en un backup externo controlado.

## Historia del proyecto

- **V2 — Fase 1.** Recolector local BTC 5m con una captura breve y una base SQLite. No hay Git ni evidencia de backtest ejecutable.
- **V3 — captura de 72 horas.** Gran evidencia raw local; el gate final quedó bloqueado por una anomalía de rango temporal, aunque la captura y sus comprobaciones documentadas son útiles como antecedente.
- **V4 — plataforma canónica.** Núcleo modular, configuraciones de bots, pruebas, investigación de wallets, clima, Elon, BTC, recompensas y PolyLedger. El repositorio Git local comenzó el 11 de septiembre de 2026.

No mover ni borrar V2/V3 para “limpiar” sin inventario y backup. Son evidencia histórica, no la base activa de desarrollo.

## Líneas de trabajo y evidencia

### 1. PolyLedger P0 — prioridad actual

**Objetivo:** reconstruir una fuente contable explicable para wallets públicas antes de evaluar, seguir o copiar una estrategia.

**Confirmado:**

- adquisición raw, cursores reorg-safe, registro/atestado de contratos, decodificadores, replay, lotes FIFO y reconciliación están implementados bajo `src/polymarket_bot/ledger/`;
- la captura de 24 horas de `car` concilió inventario observado, pero ese resultado no acredita basis ni PnL independiente;
- el bundle `data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/summary.json` está sellado como `BUNDLE_COMPLETE`;
- el bundle contiene 251.078 transacciones, 251.107 acciones, 12.211 activos, cero transacciones sin resolver y `basis_ready: true`;
- el bundle mantiene todas las salvaguardas desactivadas: solo lectura, sin firma, wallet, retiros, dinero real u órdenes.

**Resultado de ejecución actual:** el 6 de octubre se creó localmente `data/polyledger-sentinel/p0/car_lifetime_basis_result_20261006_v2/`, sin sobrescribir los `.partial` históricos. El manifiesto verifica los hashes de configuración, resumen e input, usa el commit `19bf5ac` con árbol limpio y el replay no tuvo error de reconstrucción. Conciliación de inventario: `MATCH` (3.498 activos de inventario al cierre; hash de balances `8e7c1d9de81461a7c4d6248a6440f336dcc0666380afdc8f31753ba64686cbf5`). El resumen local pesa ~122 MB y no se publica en Git.

**Bloqueo actual:** el resultado anterior no convierte el motor en aprobado. La salida está `BLOCKED` por `UNKNOWN_COST_BASIS`, marcas de cierre incompletas/sin evidencia, valor faltante de flujos externos, accrual de transferencias externo desconocido y ausencia de informe contable independiente. Las salidas `car_lifetime_basis_result_20260915_v1.partial` y `car_lifetime_basis_result_20260918_v1.partial` documentan el fallo histórico de memoria y se conservan sin cambios.

**Mejora integrada:** la optimización de memoria ya está integrada en `main`; la rama `wip/ledger-basis-memory` se conserva como traza de desarrollo. Indexa las colas FIFO por `(wallet, asset)`, sustituye la copia creciente de ancestros por vínculos de lotes, usa un diario compacto ligado por hash al bundle sellado, serializa y calcula hashes por streaming y permite `snapshot(copy_safe=False)` para una corrida única. El 7 de octubre pasaron 149 pruebas `test_ledger*`, incluidas 26 de la política de marcas, una del cursor reanudable y cuatro de la compuerta del informe independiente; una muestra real de 10.000 acciones produjo el mismo estado económico entre diario completo y compacto. En perfiles reales, cargar el bundle ocupó ~526 MB privados; el replay de 50.000 acciones llegó a ~588 MB y el de 100.000 a ~648 MB, sin duplicación material al generar la instantánea. La corrida completa de basis v2 confirmó que el arreglo elimina el bloqueo de memoria; no convierte el basis incompleto en resultado aprobado.

**Siguiente decisión técnica:** ampliar la captura CTF desde el cursor 40 con
un directorio nuevo, auditar cada resultado y crear una raíz de política propia
sin relajar identidad histórica ni la exclusión NegRisk. En paralelo deben
completarse con evidencia los flujos externos. Cuando la entrada sea suficiente
se podrá compilar un bundle nuevo; después se ejecutará el segundo cálculo
independiente y se reevaluará el gate. Eso no autoriza capital ni copia
automática.

**Inventario de brechas:** `docs/operations/POLYLEDGER_P0_EVIDENCE_GAPS_20261006.md` fija los conteos y el orden de resolución. La cola sellada local `car_lifetime_basis_evidence_gaps_20261006_v1` referencia por hash las 856 acciones de flujo y 3.498 marcas pendientes, sin duplicar el bundle ni publicar datos. No se debe volver a descargar ni repetir el bundle v5 antes de cambiar la evidencia de entrada.

**Sonda de fuentes completada:** `car_lifetime_price_probe_20261006_v2/` queda local y sellada. Validó por hash la cola de 3.498 marcas, ancló el bloque `93.762.690` con dos RPC públicos coincidentes y consultó los primeros 20 outcomes CTF de la cola con el historial oficial `as_of` un segundo antes del bloque. Cuatro candidatos quedaron dentro de la ventana explícita de una hora y dieciséis fueron antiguos; las 627 posiciones Combo y el pUSD se excluyeron como incompatibles con ese endpoint. Se guardaron 20 respuestas raw, pero se escribieron **cero** marcas y el bundle v5 no cambió. El detalle y hashes están en `docs/operations/POLYLEDGER_P0_PRICE_PROBE_20261006.md`.

**Auditoría de candidatos completada:** `car_lifetime_price_candidate_audit_20261006_v2/` verificó de nuevo la sonda y realizó 16 consultas oficiales de identidad: 16 raw, cero errores de transporte o interpretación y 4/4 mapeos oficiales actuales token–condición–outcome consistentes; dos también tienen metadata local anterior al corte. Se detectó que `markets-by-token` contradice en tres casos el orden Yes/No que documenta, por lo que el auditor trata su par como no ordenado y exige que CLOB y Gamma coincidan en las etiquetas. En esa salida del 6 de octubre los cuatro quedaron `PENDING_MARK_POLICY_REVIEW`, no como marcas aceptadas: `closing_marks_written=0`, bundle sin cambios y P0 `BLOCKED`. Detalle y hashes: `docs/operations/POLYLEDGER_P0_PRICE_CANDIDATE_AUDIT_20261006.md`.

**Política de marcas aplicada:** `car_lifetime_closing_mark_policy_20261007_v1/` volvió a verificar toda la cadena de manifests, el campo CLOB `c`, paginación, metadata pre-corte y estado CTF exacto con dRPC y Tenderly. Ambos RPC coincidieron: 4/4 condiciones no resueltas en el estado posterior del bloque de cierre. Dos candidatos pasaron `ACCEPTED_FOR_NEW_BUNDLE_COMPILATION` y dos quedaron aplazados por falta de identidad histórica anterior al corte; se integraron cero marcas y el bundle conservó su hash. Detalle: `docs/operations/POLYLEDGER_P0_CLOSING_MARK_POLICY_20261007.md`.

**Lote 21–40 cerrado:** el reintento
`car_lifetime_price_probe_20261007_batch_00021_00040_retry1/` guardó 20/20
respuestas: cinco frescas, catorce antiguas y una sin observación. La auditoría
posterior confirmó 5/5 identidades y la política específica, anclada por hashes
a ese lote, obtuvo acuerdo dual-RPC exacto. Aceptó la secuencia 27 sólo para un
bundle futuro y rechazó las otras cuatro por NegRisk; no se relajó la regla,
no se integró ninguna marca y v5 mantuvo su hash. El cursor siguiente es 40.
Detalle: `docs/operations/POLYLEDGER_P0_PRICE_BATCH_20261007.md`.

**Compuerta del segundo informe preparada:** `src/polymarket_bot/ledger/independent_report_gate.py` valida en modo offline el método, commit, evidencia y contrato exacto de un cálculo externo. La ejecución sobre `car_lifetime_basis_result_20261006_v2/` quedó sellada en `car_lifetime_independent_report_gate_20261007_v1/`: informe externo ausente, compuerta `BLOCKED`, estado P0 sin cambios. El informe independiente todavía no existe.

**Plan de fuentes:** `docs/operations/POLYLEDGER_P0_OFFICIAL_SOURCE_PLAN_20261006.md` separa historial puntual, identidad oficial, batch experimental, snapshot contable y segundo pipeline. Ningún endpoint se interpreta como basis o PnL por sí solo.

Lecturas obligatorias:

- `docs/architecture/POLYLEDGER_P0.md`
- `docs/architecture/POLYLEDGER_INVENTORY_BASIS_V1.md`
- `docs/architecture/POLYLEDGER_LIFETIME_BACKFILL_V1.md`
- `docs/operations/POLYLEDGER_P0_RUNBOOK.md`

### 2. Deportes — investigación cerrada

La línea de Deportes es independiente de PolyLedger. Su configuración es `configs/bots/sports-wallet-research.json`; el código principal vive en `sports_wallet_research.py`, `sports_wallet_copy.py` y módulos de análisis asociados.

El informe local final registra 297.752 fills, 30.962 posiciones y 22.927 señales para cuatro wallets. El candidato seleccionado antes de prueba, **Flaznorp**, terminó con PnL de -US$260,35, ROI de -5,87 % y profit factor 0,813. Conclusión registrada: **no hay ganador copiable confirmado**. Puede mantenerse un monitor shadow, pero no se debe construir un ejecutor ni recomendar copia con capital.

Evidencia: `data/sports_wallet_research_v001/final/POLYMARKET_SPORTS_FINAL_REPORT.md` y los prerregistros `docs/PREREG_SPORTS_*`.

### 3. Clima — entrada manual, no automatización

Existen investigación, backtest realista, shadow y ticket manual bajo `src/polymarket_bot/climate_*` y `configs/bots/climate-*.json`. El propósito acordado es producir una probabilidad, una explicación y un ticket que la persona evalúa y coloca manualmente; no operar de forma automática.

Antes de comunicar una “probabilidad más alta” o un resultado de backtest, ubicar el manifiesto y la salida sellada correspondientes en `data/`; no extrapolar una cifra desde un prompt o desde una cotización puntual.

### 4. Elon — conteo de publicaciones / tuits

Esta línea existe y no debe perderse: `configs/bots/elon-post-research.json`, `configs/bots/elon-post-shadow.json` y `src/polymarket_bot/elon_post_count_research.py`. El monitor forward es receive-only, de ventana máxima de 24 horas, y registra libros YES/NO, comisión, edge, capacidad y señales shadow sin enviar órdenes.

El estado del código no equivale a un resultado confirmado. Para responder sobre una ejecución concreta, identificar su run ID y leer el artefacto local sellado antes de dar PnL, acierto o recomendación.

### 5. BTC 5m, recompensas y otros experimentos

- **BTC 5m:** infraestructura y pruebas históricas/forward existen, pero los candidatos recientes quedaron bloqueados por frecuencia insuficiente. Ningún resultado habilita dinero real. La documentación conserva referencias a v0.9.1, v0.9.3 y a un parche v0.9.4a1 de watchdog; no se debe iniciar un lanzador BTC por intuición hasta que una decisión versionada fije cuál protocolo y qué base nueva son vigentes.
- **Liquidity rewards / market making:** los importes de recompensa configurados representan parámetros hipotéticos o máximos teóricos, no ganancia esperada, demostrada ni cobrable. La competencia, cola y normalización oficial pueden reducir el pago a cero.
- **Forensia de perfiles:** Balthazar, `car`, `e46m3` y `Oxp3mny` son investigaciones separadas. No se copian fills aislados: se reconstruye exposición económica, latencia, costes, liquidez y mecanismo de resolución.

## Orden de construcción acordado

1. Terminar P0 de PolyLedger.
2. Construir PolyLedger OPS Sentinel de `Oxp3mny` cuando se fije localmente su perfil y wallet pública exactos.
3. Reconstruir el mecanismo NegRisk de `e46m3`.
4. Copiado por reconstrucción de Balthazar.
5. Shadow Copy de `car`.

El backlog vivo y los criterios de salida están en `docs/roadmap/RESEARCH_IMPLEMENTATION_BACKLOG.md`. Ninguna de estas líneas debe adelantarse saltando el gate P0.

## Reglas para una continuación segura

1. Leer este documento, `CONTINUE_PROJECT.md`, el runbook específico y el estado Git antes de cambiar código.
2. Marcar toda afirmación como **confirmada**, **en investigación** o **hipótesis**; no mezclar esas categorías.
3. No usar `float` para verdad contable; usar enteros atómicos o `Decimal`.
4. Nunca sobrescribir un artefacto sellado ni eliminar un `.partial`; crear una salida con nombre nuevo y preservar hashes/manifiestos.
5. No habilitar dinero real, firma, wallet ni órdenes por un resultado de investigación, un indicador de UI o una petición ambigua.
6. No subir datos, logs, bases, resultados pesados, direcciones privadas, claves, tokens, semillas o archivos de configuración reales.
7. Para cada experimento nuevo, guardar `run_id`, configuración, versión de código, fuentes, hashes, límites y resultado.

## Estado Git y publicación

La rama de base local es `main` y ya incluye la reparación de memoria, el auditor de candidatos y la política sellada de marcas. El repositorio público vigente es `https://github.com/diegobustilloj-pixel/marketscope-ai`; los datos y salidas locales siguen excluidos. La rama `wip/ledger-basis-memory` permanece publicada como historial auditable de esa validación. Los cambios experimentales futuros deben viajar en una rama separada, no mezclarse silenciosamente con una publicación de documentación.

La publicación remota debe conservar este documento, `CONTINUE_PROJECT.md`, `README.md`, código, pruebas, configuraciones y documentación. Antes de empujar, verificar de nuevo `git status`, `.gitignore` y la ausencia de secretos. La visibilidad pública actual ya fue elegida; cualquier cambio de visibilidad o publicación de datos fuera de la política requiere confirmación explícita.

## Conflictos documentales a resolver con evidencia

| Área | Situación | Regla hasta resolverla |
|---|---|---|
| BTC 5m | El README mezcla v0.9.1/v0.9.3 y existe documentación de un parche v0.9.4a1 | No iniciar forward ni reutilizar una DB anterior sin un manifiesto de versión y smoke técnico nuevo |
| Catálogo de proyecto | `docs/PROJECT_CATALOG.md` es una foto generada, no una medición continua | Citar su fecha y regenerarlo solo cuando corresponda, sin usarlo para afirmar el estado actual de los datos |

## Decisiones aún pendientes del propietario

- Definir licencia si se compartirá públicamente; hasta entonces no se presume una licencia abierta.
- Definir política de backup externo y retención para los datos raw irremplazables.
- Autorizar por separado cualquier integración que firme, conecte una wallet, envíe una orden, retire fondos o comparta datos fuera de Git.
