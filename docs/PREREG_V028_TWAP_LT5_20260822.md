# V0.28 — diseño y preinscripción congelada

Fecha: 2026-08-22 (hora de Bolivia)

## Estado

- Variante: `V0.28_UP_LOW_VOL_TWAP_LT5_REPLICATION_24H`.
- Estado: `FROZEN_DESIGN_AWAITING_IMPLEMENTATION_AND_LAUNCH_APPROVAL`.
- Runner, auditor y entrada: no construidos.
- Autorización: ausente; `NOT_LAUNCHED`.
- Base y resultado V0.28: ausentes.
- Backtesting nuevo: 0 horas.
- Órdenes y wallet: desactivados.
- Dinero real: `BLOQUEADO`.

## Hipótesis única

Una señal pertenece a la candidata primaria sólo si satisface simultáneamente:

- El favorito implícito del mercado es `Up`.
- `0.50 < entry_cost <= 0.90`.
- `volatility_regime_ratio < 0.75`.
- `abs(twap_distance_to_open_bps) < 5.0`; el límite es estricto.
- La fuente oficial verificada del mercado usa TWAP de 60 segundos.
- Los TWAP oficiales de apertura y decisión están disponibles y frescos.
- La decisión ocurre a T-60 segundos.
- La señal usa únicamente la probabilidad implícita del mercado; el modelo transferido
  de 30 segundos permanece desactivado.

Valores ausentes, fuente ambigua, ventana distinta de 60 segundos o distancia igual a
5 bps hacen que la señal sea inelegible. No existe fallback a otra ventana TWAP.

El control padre no seleccionable conserva dirección, coste, volatilidad y fuente, y
elimina solamente el filtro de distancia TWAP. Así se mide el valor incremental del
corte de 5 bps.

## Evidencia de desarrollo

La hipótesis se eligió después de examinar trece segmentos del postmortem V0.27. Los
datos siguientes son sólo evidencia de diseño y no cuentan para validar V0.28:

- 24 operaciones; 20 victorias.
- PnL contrafactual a cinco participaciones: `+13.11513` USDC.
- Profit factor: `1.97849168`.
- ROI sobre coste: `15.094838%`.
- Primera mitad: 11 operaciones y `+10.39387625` USDC.
- Segunda mitad: 13 operaciones y `+2.72125375` USDC.
- Cuatro de seis bloques positivos.
- PnL sin la mejor operación: `+10.77755125` USDC.
- LCB unilateral 95%: `-0.0175851`.

La aproximación basada en media y dispersión post hoc da 33 operaciones para cruzar
LCB cero si el efecto se repitiera exactamente. Es planificación aproximada, no una
garantía ni un umbral de aprobación.

## Ventana y checkpoints

- Máximo: 24 horas.
- Checkpoints: 4, 8, 12, 16 y 20 horas.
- No hay éxito anticipado.
- La captura puede congelarse antes sólo por seguridad, calidad técnica o futilidad
  preinscrita de la candidata única.
- Los checkpoints no exponen outcomes ni PnL intermedio.
- La auditoría terminal será de solo lectura y se ejecutará una vez.

## Puertas finales

Frecuencia mínima:

- 20 operaciones totales.
- 8 en la primera mitad.
- 8 en la segunda mitad.

Puertas económicas simultáneas:

- PnL total positivo y profit factor mayor que uno.
- Primera y segunda mitad positivas.
- PnL positivo aun retirando la mejor operación.
- Al menos cuatro bloques evaluables, con dos operaciones por bloque.
- Al menos 60% de bloques evaluables positivos.
- Media de PnL superior al control padre.

Puerta estadística:

- Una sola hipótesis primaria.
- Alfa unilateral `0.05`, confianza `0.95` y
  `z = 1.6448536269514722`.
- El LCB unilateral debe ser estrictamente positivo.

Un resultado económico favorable con LCB no positivo puede producir
`CONTINUE_PAPER_ACCUMULATION`, pero nunca `PASS`. Sólo
`PASS_SINGLE_HYPOTHESIS_PAPER_CANDIDATE` supera todas las puertas, y tampoco habilita
dinero real automáticamente.

## Anti-overfit

- Las filas de V0.27 son exclusivamente de diseño.
- V0.28 deberá usar datos futuros independientes.
- No se permite cambiar dirección, límites, ventana, distancia, frecuencia ni puertas
  durante la ejecución.
- No se permite reutilizar V0.27 como validación.
- La preinscripción no autoriza construir ni lanzar el forward.

## Artefactos y hashes

- Evidencia de diseño:
  `data/diagnostico_v028_twap_lt5_candidate.json` —
  `1983f5a8a172cebb25bddeefcc0c7a437fd52947967d2e783623bea328906f87`.
- Preinscripción:
  `data/prereg_v028_twap_lt5_replication.json` —
  `623ec0a67da8c50bba37a51b372bde9abe59d61138576effdcfde9412b5b8a80`.
- Estrategia:
  `src/polymarket_bot/v028_strategy.py` —
  `1f9636bb79f0f7ad0dff8b22202d02d0228776b5359eb2edb8dec788cae5fb57`.
- Constructor de diseño:
  `src/polymarket_bot/v028_design.py` —
  `4c8a7e04acacbc8dac7171df98ddf5975106c4093e6b6cd51f9becad26b6abab`.
- Validador de preinscripción:
  `src/polymarket_bot/v028_prereg.py` —
  `a22401ad64bab355462b219ceab5a42bc6b24ea9ac8005b910bb4c4532ff8c9f`.

La preparación es idempotente: una segunda ejecución reutilizó los artefactos sin
reescribirlos. Las cuatro pruebas específicas y la suite completa de 265 pruebas
pasaron.

## Próximo paso seguro

Revisar la preinscripción. Sólo con una instrucción posterior explícita se podrá
construir el runner y auditor V0.28 como archivos nuevos. Esa construcción tampoco
debe lanzar el forward; el lanzamiento requerirá una autorización separada ligada por
hashes.
