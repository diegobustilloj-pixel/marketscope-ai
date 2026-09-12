# Polymarket Rewards — scanner y shadow V001

Este módulo investiga Liquidity Rewards y Maker Rebates sin conectar una billetera y sin enviar órdenes. Su salida sirve para decidir qué mercados merecen un experimento posterior; no es una señal de entrada ni una afirmación de rentabilidad.

## Qué hace

- Pagina el endpoint `rewards/markets/current` hasta el final y conserva todas las configuraciones vigentes.
- Cruza el catálogo enriquecido de `rewards/markets/multi`, separando native, sponsored y total reward cuando la API permite hacerlo.
- Refresca una vía rápida de mercados prioritarios y descarga ambos libros de outcomes.
- Lee tick, mínimo de orden, mínimo de reward, máximo spread, fees, rebate y NegRisk dinámicamente cuando están disponibles.
- Calcula el score base de nuestras órdenes hipotéticas, el midpoint filtrado por tamaño y las reglas de uno/dos lados. El multiplicador in-game queda explícitamente sin estimar cuando no está expuesto por la API.
- Compara distancias de 0.25, 0.5, 1, 1.5, 2, 3 y 4 centavos, respetando tick y máximo spread.
- Guarda cada ciclo en SQLite y genera un CSV ordenado y el reporte obligatorio.
- Se detiene si otra copia usa la misma base o si la base alcanza 5 GB.

## Qué no afirma

El libro público es agregado: no revela identidad por market maker, posición exacta en cola ni qué tamaños agregados pertenecen a órdenes individuales elegibles. Por eso `qmin_exact_ours` es el cálculo base de nuestra propuesta condicionado a `b=1`; `competitor_q_aggregate_proxy`, `gross_share_public_proxy` y los rewards esperados son proxies públicos. `net_expected_pnl` permanece vacío hasta observar fills, markouts, rebates acreditados, spread realizado e inventario. Las proyecciones se limitan al cierre o a 24 horas, lo que ocurra primero.

En particular, un daily reward muy grande en un mercado de cinco o quince minutos se prorratea por el tiempo restante. No debe leerse como un pago diario completo.

## Uso sencillo

- `actualizar_scanner_recompensas.bat`: censo completo y snapshot manual.
- `iniciar_shadow_recompensas_24h.bat`: vía rápida cada minuto durante 24 horas; requiere que exista al menos un censo completo.
- `ver_estado_recompensas.bat`: muestra el estado más reciente.

Resultados:

- `data/reward_mm_shadow_v001/reward_mm_shadow.db`
- `data/reward_mm_shadow_v001/latest_ranked_opportunities.csv`
- `data/reward_mm_shadow_v001/POLYMARKET_REWARDS_FINAL_RESEARCH_REPORT.md`

## Criterio de avance

El ejecutor de dinero real no existe deliberadamente. Solo debe diseñarse después de un periodo forward suficiente, validación cronológica fuera de muestra y un experimento real pequeño que confronte reward esperado contra reward acreditado y mida markouts de 1 s a 15 min. Hasta entonces, la decisión obligatoria es `MORE_DATA_REQUIRED` y todas las propuestas son `SHADOW_ONLY` o `NO_TRADE`.

## Fuentes oficiales vigentes

- https://docs.polymarket.com/market-makers/liquidity-rewards
- https://docs.polymarket.com/market-makers/maker-rebates
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/api-reference/rewards/get-current-active-rewards-configurations
- https://docs.polymarket.com/api-reference/rewards/get-multiple-markets-with-rewards
- https://docs.polymarket.com/api-reference/trade/get-order-scoring-status
- https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body
