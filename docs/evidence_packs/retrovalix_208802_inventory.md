# Evidence Pack — inventario pareado y skew direccional

**Veredicto:** `FAIL`

**Alcance del veredicto:** Falla la interpretación mínima comprobada por V0.16/V0.17; no se afirma que haya fallado el algoritmo exacto de nagi777 ni que su PnL público sea falso.

**Post fuente:** [2088025556428501021](https://x.com/RetroValix/status/2088025556428501021) — 2026-08-13

## Afirmación pública resumida

El post atribuye +$126.836 a un bot que coloca límites en UP y DOWN, forma inventario pareado por debajo de $1 y conserva un residual direccional.

## Alcance y distinción obligatoria

El post no publica reglas suficientes para reconstruir el bot. QuantBot probó una interpretación mínima y congelada: comprar primero el lado barato y completar el par más tarde solo si el coste ejecutable conjunto queda en 0,97 o menos. El veredicto se limita a esa interpretación.

**Interpretación:** Hecho observado: texto y métricas del post. Interpretación: V0.16 modela la rotación temporal más sencilla compatible con la narrativa. No hay identidad demostrada entre V0.16 y el algoritmo del trader.

## Especificación comprobable

- **Universo:** Mercados BTC UP/DOWN de cinco minutos presentes en la fuente Silver sellada.
- **Momento de decisión:** Entrada entre los segundos 30 y 210; búsqueda de hedge posterior hasta el segundo 285.
- **Entradas:**
  - Comprar cinco shares del lado con coste ejecutable máximo 0,30 en CAUTIOUS_030.
  - Exigir BBA y profundidad CLOB válidos con (quality_flags & 22) == 0.
  - Permitir como máximo una posición por mercado.
- **Cobertura:**
  - Comprar cinco shares del lado opuesto solo en un segundo posterior.
  - Aceptar el hedge únicamente cuando el coste conjunto ejecutable sea 0,97 o menor.
- **Salidas:**
  - Mantener el par completo hasta resolución.
  - Si no aparece hedge, mantener la primera pierna hasta resolución.
- **Supuestos de ejecución:**
  - Usar ask más 0,005 de slippage por share.
  - Aplicar comisión 0,07*p*(1-p).
  - Exigir profundidad mínima para cinco shares dentro de un centavo del mejor ask.

## Desconocidos

- Modelo direccional exacto de nagi777.
- Prioridad de cola, cancelaciones y partial fills de sus órdenes límite.
- Regla real de sizing y del residual direccional.
- Rebates, latencia, infraestructura y pérdidas no realizadas del trader.

## Comprobaciones ejecutables

| Comprobación | Tipo | Fuente | Campo | Resultado |
|---|---|---|---|---|
| El post describe límites simultáneos UP/DOWN | identity | data/retrovalix_posts_raw.json | detailText | PASS (This guy built a trading bot and made +$126,836 on Polymarket  I analyzed its trades, and it trades like a high-frequency market maker on 5-minute crypto Up/Down markets  Its algorithm works like this:  1. Places limit orders on Up and Down at the same time  2. Uses fluctuations in the underlying asset to build complete sets at different moments  The average cost of this kind of set is around 98.43c  This means the matched positions are formed with roughly a 1.57c edge  3. Leaves a directional skew on the side it sees as more likely  Around 78.7% of the capital goes into paired Up+Down inventory  The other 21.3% is used as directional residual on one side  Stats:  > Trades/active hour: 51.25 > Average trade: $110.67 > Win Rate: 50%  His Polymarket nickname: nagi777  Using this strategy, he captures a consistent edge. Then he repeats this thousands of times and keeps growing his capital) |
| V0.16.1 terminó FAIL_DEVELOPMENT | failure_gate | data/resultado_v0161_inventory_rotation.json | verdict | PASS (FAIL_DEVELOPMENT) |
| V0.16.1 no seleccionó candidato | failure_gate | data/resultado_v0161_inventory_rotation.json | selected_candidate | PASS (null) |
| CAUTIOUS_030 tuvo PnL neto negativo | failure_gate | data/resultado_v0161_inventory_rotation.json | development_results.CAUTIOUS_030.net_pnl | PASS (-30.814115) |
| V0.17 también terminó FAIL_DEVELOPMENT | failure_gate | data/resultado_v017_hedge_completion_development.json | verdict | PASS (FAIL_DEVELOPMENT) |
| V0.17 no abrió la validación sellada | safety | data/resultado_v017_hedge_completion_development.json | sealed_validation_read | PASS (False) |
| El forward activo no fue leído | safety | data/resultado_v0161_inventory_rotation.json | forward_active_read | PASS (False) |

## Métricas observadas

| Métrica | Valor | Fuente |
|---|---:|---|
| Entradas CAUTIOUS_030 | 165 | data/resultado_v0161_inventory_rotation.json |
| Tasa pareada CAUTIOUS_030 | 62.42% | data/resultado_v0161_inventory_rotation.json |
| PnL neto CAUTIOUS_030 | -30.814115 | data/resultado_v0161_inventory_rotation.json |
| ROI CAUTIOUS_030 | -5.49% | data/resultado_v0161_inventory_rotation.json |
| Mejor AUC de V0.17 | 0.63927739 | data/resultado_v017_hedge_completion_development.json |
| PnL V0.17 C0.1 umbral 0,80 | 1.41504625 | data/resultado_v017_hedge_completion_development.json |
| PnL segunda mitad V0.17 C0.1 umbral 0,80 | -1.635045 | data/resultado_v017_hedge_completion_development.json |

## Razones del veredicto

- Las tres configuraciones V0.16.1 tuvieron PnL y ROI negativos pese a formar pares rentables.
- Las piernas sin hedge perdieron más que lo ganado por el inventario pareado.
- V0.17 elevó la tasa pareada en algunos umbrales, pero ninguna combinación cumplió estabilidad temporal.
- No se abrió el holdout sellado ni se rescató un umbral después del fallo.

## Evidencia que falta

- Reglas exactas del algoritmo público de nagi777.
- Historial completo de órdenes, cancelaciones y prioridad de cola.
- Modelo de probabilidad que determina el skew direccional.
- Rebates, latencia y capital operativo reales.

## Siguiente prueba válida

- No rescatar V0.16/V0.17 cambiando umbrales.
- Capturar como máximo 24 horas de órdenes/fills paper bajo el régimen CLOB actual.
- Congelar una regla nueva de salida para la pierna incompleta antes de evaluar PnL.
- Mantener el resultado separado del algoritmo de nagi777 salvo que aparezcan reglas verificables.

## Seguridad

- Wallet: no requerida.
- Órdenes: bloqueadas.
- Dinero real: bloqueado.
- Forward activo: no leído y no modificado.
- Experimento nuevo: no abierto.
- Duración máxima futura: 24 horas.

## Provenance

- Manifest SHA-256: `ad2789e930e65e8de4e97fad64f41817723fe793898fe152dbb942df374d174b`
- `data/retrovalix_posts_raw.json` — `1793412f7ef5d1fd765064c07bdaaf87df52e9996865e611ac5b91e739fd9653` — Texto del post fuente registrado desde X.
- `data/prereg_v0161_inventory_rotation.json` — `b7a361fcf1257c95dca82ac11dfea8238de7ccdfe9abcf9678aadd5879d75123` — Corrección técnica congelada antes de evaluar rentabilidad.
- `data/resultado_v0161_inventory_rotation.json` — `511b3a41fbaca558fa9eea493b111c1405f584356aeacfb2868c0a907e5d784f` — Resultado sellado de la interpretación mínima.
- `data/prereg_v017_hedge_completion_development.json` — `8938cb9af3aa0a80ee32fce8bba9803ea5fc83a8b534eb02c6660e1956913f91` — Protocolo congelado del filtro de probabilidad de completar hedge.
- `data/resultado_v017_hedge_completion_development.json` — `f25c6a56bf2bf223901db6b33786d25cf0c1571a1878da3d823ea8980f79e5e3` — Resultado sellado del intento de controlar la pierna incompleta.
