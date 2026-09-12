# Postmortem final V0.27 — UP baja volatilidad

Fecha: 2026-08-22 (hora de Bolivia)

## Alcance e integridad

Este análisis usa exclusivamente la base y el resultado final sellados de V0.27. La
base se abrió en modo de solo lectura y no se ejecutó ningún backtest nuevo.

- Resultado fuente: `FAIL_NO_ECONOMIC_REPLICATION`.
- Base V0.27 SHA256:
  `9aa2fb704e34a9bc297b9d1e398deb588a388a3d246ed3cc27014aea2057132b`.
- Resultado V0.27 SHA256:
  `2f0d6db129a5d307899361cf417cec4d5503f71c695f93a8c082c0e3542feeb9`.
- Postmortem SHA256:
  `8cfe259ffc204468678c4d00d12e42fc654cb35720855826beecca06a8dd5f01`.
- Preinscripción, implementación y `phase41.py`: sin cambios.
- Órdenes, wallet y dinero real: bloqueados.

El análisis reutiliza particiones definidas antes de V0.27: bloques de cuatro horas,
sesiones UTC, bandas de coste, bandas de distancia TWAP y alineación TWAP. Se
examinaron trece segmentos. Por tanto, cualquier hallazgo segmentado es exploratorio
y no constituye validación ni selección.

## Diagnóstico del candidato completo

`favorite_up_low_vol_lt_075` tuvo 28 operaciones:

- PnL contrafactual a cinco participaciones: `+0.643725` USDC.
- Profit factor: `1.02487843`.
- ROI sobre coste: `+0.647896%`.
- Primera mitad: `+3.54542375` USDC.
- Segunda mitad: `-2.90169875` USDC.
- Bloques positivos: 3 de 6 (`0.50`; se exigía `0.60`).
- LCB Bonferroni: `-0.15732381`.
- PnL sin la mejor operación: `-1.69385375` USDC.

La ganancia completa es marginal, no estable y depende de conservar la mejor
operación. Por ello el candidato original no queda rescatado ni replicado.

## Efecto del filtro de volatilidad

El control UP amplio tuvo 38 operaciones y `-4.3454775` USDC a cinco
participaciones. El candidato conservó 28 de esas operaciones. Las diez excluidas por
el filtro de baja volatilidad sumaron `-4.9892025` USDC, con profit factor
`0.62461473`.

El filtro de volatilidad mejoró esta ventana cerrada, pero no bastó para dar
estabilidad al candidato completo.

## Único segmento que superó el filtro exploratorio

La partición preexistente `abs(twap_distance_to_open_bps) < 5` fue el único segmento
de trece que superó todas las puertas diagnósticas conservadoras:

- Regla base: favorito UP, `0.50 < coste <= 0.90`, volatilidad relativa `< 0.75`.
- Condición adicional: distancia absoluta entre TWAP oficial de decisión y TWAP de
  apertura `< 5 bps`.
- Operaciones: 24; victorias: 20; win rate: `83.33%`.
- PnL a cinco participaciones: `+13.11513` USDC.
- Profit factor: `1.97849168`.
- ROI sobre coste: `15.094838%`.
- Primera mitad: `+10.39387625` USDC, 11 operaciones.
- Segunda mitad: `+2.72125375` USDC, 13 operaciones.
- Seis bloques representados; cuatro positivos (`66.67%`).
- PnL sin la mejor operación: `+10.77755125` USDC.
- LCB unilateral 95%: `-0.0175851`.

Las cuatro operaciones complementarias con distancia de 5 a 10 bps perdieron todas y
sumaron `-12.471405` USDC. No hubo operaciones con distancia de al menos 10 bps.

El corte de 5 bps tiene una interpretación disponible en tiempo real: sólo usa el
TWAP oficial de 60 segundos observado a T-60 y el TWAP oficial de apertura. No usa el
resultado futuro. Aun así, fue elegido después de examinar varios segmentos y su LCB
sigue ligeramente por debajo de cero.

Si la media y dispersión observadas se repitieran exactamente, una aproximación
normal requeriría unas 33 operaciones para que el LCB unilateral 95% superara cero,
nueve más que las 24 observadas. Es una estimación de planificación, no una garantía.

## Decisión

- `favorite_down_cost_070_080`: descartar, confirmado por V0.27.
- `favorite_up_low_vol_lt_075`: hipótesis solamente; no rescatada ni replicada.
- Segmento UP/TWAP menor de 5 bps: candidato de diseño para una prueba fresca, no
  estrategia aprobada.
- V0.28: no lanzar todavía. El siguiente paso admisible es construir una
  preinscripción confirmatoria con una sola hipótesis y datos futuros independientes.
- Ninguna conclusión habilita órdenes ni dinero real.

## Implementación reproducible

- Módulo: `src/polymarket_bot/v027_final_postmortem.py`.
- Entrada: `v027_postmortem_report.py`.
- Resultado: `data/postmortem_v027_up_low_vol_final.json`.
- Pruebas específicas: 3.
- Suite completa del proyecto: 261 pruebas correctas.
