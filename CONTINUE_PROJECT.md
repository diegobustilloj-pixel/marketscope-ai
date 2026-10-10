# Guía de relevo — cómo continuar sin reiniciar el proyecto

Esta guía está pensada para la siguiente persona o agente que trabaje en la plataforma. Su objetivo es evitar que se repitan análisis, se pierda la línea de Elon, se confundan Deportes y PolyLedger, o se convierta una investigación en una orden real.

## Lectura obligatoria antes de actuar

1. `PROJECT_STATE.md` — panorama, decisiones y evidencia disponible.
2. `git status --short` y `git log --oneline -12` — identificar cambios no consolidados y el punto real de partida.
3. `docs/architecture/PROJECT_ORGANIZATION.md` y `docs/operations/DATA_POLICY.md` — límites de estructura y datos.
4. El documento del dominio que se vaya a tocar. Para el trabajo actual: los documentos PolyLedger enlazados en `PROJECT_STATE.md`, incluida la sonda y su auditoría de candidatos si se retoman marcas históricas.

Si falta una fuente, un run ID o un hash, declararlo como ausencia de evidencia; no rellenarlo con una suposición.

## Punto exacto de continuación: PolyLedger P0

La prioridad es terminar de forma verificable el cálculo de inventario y basis de la wallet pública `car`, no abrir una nueva estrategia.

### Lo que ya está hecho

- El bundle histórico v5 existe localmente: `data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5`.
- Su `summary.json` declara `BUNDLE_COMPLETE`, `basis_ready: true`, 251.078 transacciones, 251.107 acciones y cero acciones sin resolver.
- El inventario reconciliado acredita cantidades observadas, pero no equivale a basis ni a PnL realizado.
- Dos resultados de inventory-basis se conservan como `.partial` porque el proceso agotó memoria. Son evidencia de bloqueo, no basura temporal.
- La corrida de basis v2 ya existe localmente en `data/polyledger-sentinel/p0/car_lifetime_basis_result_20261006_v2`. Su manifiesto, hashes y archivo de configuración fueron verificados; la reconstrucción no tuvo error y la conciliación de cierre dio `MATCH`. El resultado sigue `BLOCKED` por evidencia de basis/PnL, no por un error de ejecución.

### Cambio integrado y validado

La optimización ya está en `main`; `wip/ledger-basis-memory` permanece como
traza de desarrollo. Toca:

- `src/polymarket_bot/ledger/common.py`
- `src/polymarket_bot/ledger/inventory_basis.py`
- `src/polymarket_bot/ledger/lots.py`
- `tests/test_ledger_inventory_basis.py`
- `tests/test_ledger_lots.py`

El cambio introduce un índice FIFO por wallet/activo y conserva una cadena de `lineage_lot_ids` en lugar de expandir toda la procedencia ancestral en cada lote descendiente. También evita copiar el bundle al validarlo/hashearlo, usa un diario contable compacto enlazado por hash al input sellado, no duplica el bundle de 200+ MB en `configuration.json` y serializa resultados por streaming. Busca reducir coste temporal y memoria sin cambiar el resultado contable.

Las 149 pruebas `test_ledger*` pasaron el 7 de octubre de 2026; 26 cubren la política de marcas, una el cursor reanudable de lotes y cuatro la compuerta del informe independiente. Una muestra real de 10.000 acciones comparó diario completo frente a compacto y conservó exactamente el mismo estado económico. En el mismo bundle, los perfiles de 50.000 y 100.000 acciones usaron aproximadamente 588 MB y 648 MB privados, respectivamente, incluida la carga de ~526 MB del bundle; la instantánea no duplicó materialmente la memoria. La corrida completa de basis v2 se selló sin error de reconstrucción y con conciliación `MATCH`. El próximo trabajo ya no es validar escala, identidad de la muestra ni la política de aceptación:

1. conservar la salida de basis v2 y sus hashes; no volver a correr el bundle salvo que cambie el motor o la evidencia de entrada;
2. conservar la sonda, auditoría y política iniciales, además de las salidas
   selladas 21–40 y 41–60 enlazadas desde
   `docs/operations/POLYLEDGER_P0_PRICE_BATCH_20261007.md`: entre diez
   candidatos, cuatro precios fueron aceptados sólo para una futura
   compilación, dos quedaron aplazados y cuatro rechazados (sonda inicial:
   2/2/0; lote 21–40: 1/0/4; lote 41–60: 1/0/0, en orden
   aceptados/aplazados/rechazados); cero marcas fueron integradas y v5 no
   cambió;
3. reanudar las secuencias 61–80 por lotes de hasta 20 desde el cursor 60 de
   `probe-price-history`, crear auditoría y raíz de política nuevas para cada
   lote, y completar 856 flujos externos; cuando la entrada esté completa,
   compilar un bundle nuevo, ejecutar el cálculo independiente, pasarlo por
   `verify-independent-report` y reevaluar el gate. Nunca sobrescribir v5 ni
   borrar los `.partial` existentes.

No interpretar que el índice por sí solo resuelve toda la memoria: `apply_batch` y los snapshots pueden copiar estructuras amplias. Si el problema persiste, perfilar primero y cambiar una sola fuente de duplicación por vez, manteniendo un replay determinista y la trazabilidad de cada lote.

## Secuencia técnica recomendada

```text
resultado de basis v2 sellado y conciliado
    → políticas de marcas selladas hasta cursor 60: ampliar captura desde 60
    → completar flujos externos y marcas de cierre con evidencia
    → recompilar un nuevo bundle sin tocar v5
    → verificar balances, hash y PnL contra un cálculo independiente
    → decidir si P0 sigue BLOCKED o avanza
```

Un resultado `COMPLETE` de un motor no basta: P0 exige conciliación de inventario, basis, PnL y un segundo pipeline independiente. En ningún punto esa aprobación habilita órdenes, capital, firma o conexión de wallet.

## Qué no se debe repetir

- No volver a descargar o reconstruir el universo histórico ya cerrado solo porque una corrida de basis falló; usar el bundle sellado y crear salidas nuevas.
- No llamar “ganancia” a cambio de inventario, MTM o recompensa teórica sin conciliación y definición de costes.
- No declarar que Deportes es rentable: su finalista falló la prueba y el informe final cerró la línea como no desplegable.
- No olvidar Elon: hay investigación y monitor shadow; buscar su run ID local antes de responder sobre resultados.
- No tratar Balthazar, `car`, `e46m3` y `Oxp3mny` como la misma cartera o la misma estrategia.
- No iniciar un lanzador BTC solo porque aparezca en el README: existen referencias a v0.9.1, v0.9.3 y v0.9.4a1. Primero fijar con evidencia cuál protocolo, watchdog y base nueva son vigentes.
- No extraer datos masivos de `data/` a Git, ni publicar credenciales, tokens, claves, semillas, bases, logs o capturas raw.

## Mapa de decisiones posteriores a P0

| Orden | Construcción | Condición de entrada |
|---:|---|---|
| 2 | OPS Sentinel de `Oxp3mny` | Perfil y wallet pública exactos fijados en evidencia local |
| 3 | Reconstrucción NegRisk de `e46m3` | Decodificador/ledger común preserva conversiones y collateral |
| 4 | Copiado por reconstrucción de Balthazar | Backtest event-time, costes y shadow forward antes de ticket manual |
| 5 | Shadow Copy de `car` | Muestra preregistrada con latencia, slippage y PnL neto shadow |

Estas etapas no son permisos de trading. Cada una debe empezar en solo lectura o shadow y presentar evidencia reproducible.

## Convención para nuevos cambios

- Un cambio por objetivo; no mezclar refactorización masiva con una reparación contable.
- Pruebas junto al cambio y salida explícita de verificación.
- Ramas sugeridas: `docs/...`, `fix/...`, `feat/...` y `wip/...` para trabajo experimental no validado.
- Commits que expliquen qué evidencia cambia, qué sigue bloqueado y cómo reproducir la verificación.
- Las decisiones que alteren el orden técnico, el riesgo o la publicación se anotan en `PROJECT_STATE.md` y, si corresponde, en el backlog.

## Antes de entregar o publicar

1. Confirmar el estado de los bots: todos deben seguir con dinero real y órdenes automáticas desactivados, salvo autorización explícita independiente.
2. Ejecutar comprobación de secretos y revisar `.gitignore`.
3. Verificar que no se incluyeron `data/`, `logs/`, archivos `.partial`, bases o artefactos pesados.
4. Escribir un resumen breve de evidencia, limitaciones y siguiente paso en vez de prometer rentabilidad.
5. No publicar a un repositorio público sin que el propietario haya elegido expresamente ese destino y visibilidad.
