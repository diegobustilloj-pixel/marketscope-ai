# Plataforma cuantitativa Polymarket

> **Proyecto canónico:** `C:\ProyectoBotV4\polymarket_quant_bot`  
> **Política:** una plataforma, un núcleo compartido y múltiples bots declarados por configuración. Las rutas y lanzadores históricos permanecen compatibles durante la migración.

## Centro de control

```powershell
# Validar el catálogo profesional de bots
.\.venv\Scripts\python.exe manage_bots.py validate

# Listar bots, dominio, etapa y seguridad
.\.venv\Scripts\python.exe manage_bots.py list

# Ver un bot concreto
.\.venv\Scripts\python.exe manage_bots.py show polyledger-sentinel

# Regenerar el inventario de bots, archivos y datos
.\.venv\Scripts\python.exe tools\build_project_catalog.py
```

- Organización: `docs/architecture/PROJECT_ORGANIZATION.md`.
- Operación segura: `docs/operations/RUNBOOK.md`.
- Política de datos: `docs/operations/DATA_POLICY.md`.
- Inventario generado: `docs/PROJECT_CATALOG.md`.
- Backlog de investigación a construcción: `docs/roadmap/RESEARCH_IMPLEMENTATION_BACKLOG.md`.
- Catálogo: `configs/bots/*.json`.
- Ambientes: `configs/environments/*.json`.
- Migración del legado: `docs/migrations/LEGACY_LAYOUT_MIGRATION.md`.

El catálogo **no inicia procesos ni habilita dinero real**. Solo registra y valida. Todo bot nuevo nace `draft`, con `real_money=false` y `automatic_orders=false`.

## Línea histórica: BTC 5m — Fase 4.2

> Estado actual: **v0.9.3 experimental**. Añade un candidato de transferencia TWAP para saltar la espera previa: reutiliza el HGB strike congelado, pero sustituye la distancia al strike antiguo por la distancia entre TWAP 30 s actual y TWAP 30 s de apertura. Sólo paper/shadow; dinero real sigue bloqueado.


Sistema profesional en desarrollo, completamente **sin Claude, sin LLM, sin
wallet y sin dinero real**.

## PolyLedger Sentinel v0.1

Auditor separado y de solo lectura para wallets públicas de Polymarket. Conserva actividad incremental en SQLite, distingue conversiones internas de PnL, cruza posiciones con `/value` y leaderboard, detecta inconsistencias y exporta todas las posiciones a CSV. Inicie `ejecutar_polyledger_sentinel.bat` o consulte `docs\POLYLEDGER_SENTINEL_MVP_V001.md`.

**P0 en construcción:** el núcleo `polymarket_bot.ledger` añade raw inmutable,
cursores atómicos y reorgs, contratos/ABI versionados, lotes contables exactos y
reconciliación CLOB/ledger/onchain. Incluye captura RPC y replay determinista.
El PositionManager de Combo ya tiene despliegue/implementación calificados,
transferencias ERC-1155 e IDs estructurales; no se infiere todavía la economía
de sus módulos. La aprobación P0 continúa bloqueada por esos mapeos y por la
evidencia independiente del piloto de 24 horas. Operación y comandos:
`docs/operations/POLYLEDGER_P0_RUNBOOK.md`; diseño y límites:
`docs/architecture/POLYLEDGER_P0.md`. El Sentinel histórico sigue compatible.

La siguiente fase acordada es **Copiado por reconstrucción**: observar una wallet,
reconstruir la cesta completa y cotizar si todavía es replicable antes de mostrar
un ticket manual. El diseño congelado y sus reglas de seguridad están en
`docs\PLAN_COPIADO_POR_RECONSTRUCCION_V001.md`. Su estado es pendiente de
construcción; no activa órdenes ni dinero real.

## Monitor Elon post-count V0.01

El sector Cultura dispone de un monitor forward separado, receive-only y con
duración máxima de 24 horas. Usa el ensemble congelado Negative Binomial +
ventanas históricas comparables, registra libros YES/NO, comisión de Cultura,
edge, capacidad visible y señales shadow. Nunca crea ni envía órdenes.

- `iniciar_elon_shadow_24h.bat`: inicia la ventana de 24 horas.
- `ver_estado_elon_shadow.bat`: muestra progreso y últimos pronósticos.
- `detener_elon_shadow.bat`: solicita cierre limpio.
- Protocolo: `docs\PREREG_ELON_SHADOW_FORWARD_V001_20260901.md`.

## Evidencia aprobada

Las Fases 1 y 2 terminaron satisfactoriamente:

- V3: 71,999 horas, 109.617.113 eventos, 865 mercados consecutivos y cero
  corrupción;
- V4: validación de dos horas aprobada con Binance directo, Chainlink, CLOB y
  Gamma;
- Silver: 267.000 filas, 890 etiquetas verificadas y cero eventos malformados;
- 885 mercados superaron el umbral individual de cobertura para modelado;
- todas las bases raw y Silver permanecen inmutables.

## Dataset Gold aprobado

Gold v2 conservó correctamente los 885 mercados aptos, produjo 3.538 de 3.540
filas esperadas, obtuvo 99,9435% de cobertura y no registró fugas temporales.
Los 348 mercados sin `priceToBeat` oficial se conservaron con sus variables de
strike en `NULL`.

## Resultado Fase 4

La Fase 4 comparó seis modelos y Polymarket en cuatro horizontes. El proceso
fue correcto, pero ningún candidato superó el límite de confianza ajustado.
El test histórico permaneció bloqueado. Tres configuraciones de 60 segundos
mostraron resultados prometedores y pasan ahora a investigación forward.

## Objetivo de esta entrega

La versión 0.9.1 implementa la Fase 4.2 sobre la evidencia de Fase 4.1:

- mantiene congelados los tres modelos y sus thresholds;
- corrige la captura de `priceToBeat` desde Gamma Event con fallback;
- convierte timeouts de Gamma en errores recuperables;
- reconcilia runs `RUNNING` y features `PENDING` tras cortes;
- añade diagnósticos a 120/30/15 s sin alterar la señal oficial de 60 s;
- fuerza una base shadow nueva (schema v2) para no reutilizar Fase 4.1;
- continúa sin wallet, sin dinero real y sin órdenes.

## Seguridad

- Las bases Silver se abren con `mode=ro`.
- Nunca se sobrescribe una salida existente.
- Una ejecución incompleta queda con extensión `.partial`; solo una salida
  aprobada recibe el nombre definitivo.
- No solicita claves, API keys, wallet ni seed phrase.
- No crea, firma ni envía órdenes.
- No usa Kelly ni dimensiona posiciones.

## Actualización en Windows

1. Cierre cualquier ventana CMD del bot.
2. Extraiga el ZIP nuevo encima de
   `C:\ProyectoBotV4\polymarket_quant_bot`.
3. Seleccione **Reemplazar los archivos en el destino**.
4. Ejecute `instalar_windows.bat`.
5. Verifique que la versión instalada sea Fase 4.2 v0.9.1.

No extraiga esta versión en una carpeta separada: Gold necesita encontrar las
dos bases Silver aprobadas dentro de `data`.

## Fase 2 aprobada

El procesamiento completo seleccionó 862 mercados V3 y 23 mercados V4. Los
cinco mercados incompletos se conservan en Silver, pero no entrarán a Gold.

## Estado de Fase 4.1 y gate de Fase 4.2

La prueba de siete días de Fase 4.1 ya terminó. La infraestructura fue válida,
pero los modelos combinados fueron rechazados por rentabilidad/calibración y el
modelo strike no fue evaluable por falta de `priceToBeat`.

**No iniciar todavía otra prueba de siete días.** Primero debe realizarse una
prueba técnica corta de Fase 4.2 sobre una base nueva y verificar strike,
timeouts, reanudación y diagnósticos. Consulte `docs\FASE_4_2_CORRECCIONES.md`.

## Comandos principales

| Comando | Función |
|---|---|
| `prepare-shadow` | Congela las tres hipótesis sin abrir test |
| `run-shadow` | Ejecuta o reanuda forward testing ligero |
| `shadow-status` | Consulta progreso sin modificar la prueba |
| `audit-shadow` | Audita cobertura, probabilidades y PnL forward |
| `paper-risk-status` | Audita el perfil de riesgo paper-only sin activarlo |
| `supervisor_quantbot.py --status` | Consulta recuperación de procesos sin leer outcomes |
| `estado_general.py` | Panel seguro con progreso y horas estimadas |
| `experiment_finalizer.py --status` | Consulta el cierre automatico sin modificar nada |
| `v015_monitor.py --status` | Consulta la confirmacion condicional v0.15 |
| `analizar_resultados_actuales_24h.py` | Resume evidencia sellada bajo la política máxima de 24h |
| `investigar_v016_inventory_rotation.py` | Reproduce la prueba histórica de inventario temporal UP/DOWN |
| `preparar_v017_hedge_completion.py` | Reproduce el filtro de probabilidad de completar el hedge |
| `build-models` | Entrena, calibra, selecciona y hace backtest bloqueado |
| `phase4-status` | Verifica el experimento y sus gates |
| `build-gold` | Crea features y particiones temporales Gold |
| `gold-status` | Verifica y resume Gold |
| `build-silver` | Construye una base Silver desde V3/V4 |
| `silver-status` | Verifica y resume una base Silver |
| `export-v3` | Exporta muestras pequeñas de V3 |
| `audit` | Audita una captura raw |
| `verify-fast` | Comprueba integridad raw |

## Estado

- [x] Captura raw auditable V3/V4.
- [x] Binance público directo.
- [x] Resoluciones Gamma verificables.
- [x] Procesador streaming con memoria limitada.
- [x] Esquema Silver por segundo.
- [x] Pruebas sintéticas automatizadas.
- [x] Piloto real de 10 mercados V3 + 10 mercados V4.
- [x] Procesamiento completo de V3 y V4.
- [x] Dataset Gold y división temporal sin fuga.
- [x] Motor de modelos probabilísticos comparables.
- [x] Backtesting con comisiones y slippage.
- [x] Forward shadow de siete días Fase 4.1 (infraestructura aprobada; modelos rechazados).
- [x] Correcciones de infraestructura Fase 4.2 implementadas y cubiertas por tests.
- [x] Motor de riesgo paper-only fail-closed (perfil actual bloqueado).
- [x] Supervisor de recuperación fail-closed para los procesos paper-only.
- [ ] Prueba técnica corta Fase 4.2 en base nueva.
- [ ] Nueva ventana forward independiente.
- [ ] Paper trading.

Consulte `docs\FASE_4_2_CORRECCIONES.md` para las correcciones actuales,
`docs\FASE_4_1_FORWARD.md` para el contrato forward original y
`docs\FASE_4_MODELOS.md` para el experimento histórico.


## Fase 4.2 v0.9.1 - transición a TWAP de 30 segundos

Desde el 7 de agosto de 2026 los mercados cripto de 5 minutos usan un TWAP de 30 segundos para resolución. Esta versión no intenta reutilizar el modelo de strike entrenado bajo la regla anterior.

Cambios de seguridad y datos:

- RTDS se suscribe simultáneamente a `crypto_prices_chainlink` y `crypto_prices_twap_thirty`.
- Cada TWAP BTC/USD de 30 segundos se persiste en `shadow_twap_ticks` con timestamp, valor, precisión completa y ventana.
- Las features oficiales y diagnósticas incorporan campos `twap_30s_*` sin modificar las variables de entrada de los modelos heredados.
- Se evita look-ahead: una feature solo puede usar TWAP con timestamp menor o igual a su instante de decisión.
- `strike_hist_gradient_boosting` queda deshabilitado explícitamente bajo el nuevo régimen.
- `audit-shadow` nunca puede devolver `forward_candidate=true` mientras `twap_retraining_required=true`.
- El esquema Shadow pasa a versión 3; use una base nueva.

Antes de cualquier nueva ventana de siete días, ejecute `iniciar_prueba_tecnica_fase42_v091.bat` y revise el resultado con `ver_progreso_fase42_v091.bat`.

## Preparación de riesgo paper-only

La capa de riesgo de cartera está implementada de forma independiente y no
está conectada a las pruebas v0.13/v0.14. El perfil actual permanece en
`DRAFT_BLOCKED`: conserva solamente los límites derivados de los
prerregistros y rechaza su activación mientras falten límites respaldados por
evidencia.

Para comprobar el estado sin leer outcomes ni modificar las pruebas activas:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot paper-risk-status
```

Consulte `docs\FASE_5_RIESGO_PAPER.md` para el contrato de seguridad y los
bloqueos pendientes. Wallet, órdenes y dinero real continúan deshabilitados.

## Recuperación automática

El supervisor separado recupera los seis procesos paper-only base y administra
dos workers v0.15 condicionales después de una interrupción. V0.15 no arranca
hasta que el resultado sellado v0.14 seleccione una banda válida. El cierre
automático audita el forward solo tras su finalización oficial. No modifica los
protocolos congelados v0.13/v0.14 ni permite lectura parcial de labels. Consulte
`docs\SUPERVISOR_RECUPERACION.md` para sus protecciones y registros.

Para ver todo el avance y sus horas estimadas desde CMD:

```cmd
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe estado_general.py
```

El contrato completo de la confirmación futura está en
`docs\PROTOCOLO_V015.md`.

## Política máxima de 24 horas

Desde el 14 de agosto de 2026, todo experimento o backtest temporal nuevo está
limitado a 24 horas. `run-shadow` usa 24 horas por defecto y rechaza duraciones
mayores. El forward de siete días que ya estaba iniciado conserva una única
excepción de reanudación para su base exacta y termina el 16 de agosto a las
20:02 de Bolivia; no autoriza nuevas pruebas largas.

V0.13 y V0.14 terminaron por frecuencia insuficiente con cero labels leídos;
V0.15 no aplica. La familia TWAP-shock/market-lag queda cerrada sin rescate de
umbrales. Consulte `docs\POLITICA_24H_Y_RESULTADOS_20260814.md`.

## Investigación de inventario inspirada en el post de X

V0.16/V0.16.1 probaron rotación temporal de cinco shares sobre 24 horas
históricas. La frecuencia fue suficiente, pero las piernas que no consiguieron
hedge hicieron negativo el resultado. V0.17 elevó la tasa de pares con un
clasificador, aunque falló el control de estabilidad temporal. Ambos quedan
cerrados en desarrollo; no se abrió el holdout final ni se habilitó paper
trading. Consulte `docs\POST_X_INVENTARIO_RESULTADOS_20260814.md`.
