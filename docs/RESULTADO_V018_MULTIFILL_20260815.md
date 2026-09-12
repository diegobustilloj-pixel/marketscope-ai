# Resultado oficial V0.18 maker multifill

V0.18 termino limpiamente despues de 24 horas exactas. El resultado oficial es `FAIL_BEHAVIOR_REPLICATION`. Por el orden de decision congelado, el fallo decisivo fue `WEIGHTED_COMPLETE_SET_COST`. La evaluacion de rendimiento, calculada igualmente como diagnostico post-completion, tambien fallo.

## Integridad

- Ventana: 2026-08-14 15:35:16 a 2026-08-15 15:35:16, hora de Bolivia.
- Mercados completos: 287/287.
- Mercados interrumpidos: 0.
- Cobertura minima: 294/300 segundos; promedio: 297.979/300.
- Quotes paper: 4.872; fills paper: 2.363.
- SQLite quick check: `ok`.
- Ordenes reales y filas de dinero real: 0.
- Outcomes: 287, consultados una sola vez despues de `experiment_completed_at`.
- Forward principal: no leido ni modificado.

## Gates pre-outcome

Seguridad, calidad tecnica y frecuencia pasaron. Los 287 mercados tuvieron fills y posicion pareada. El capital pareado agregado fue 89,94%, el residual maximo fue 5 shares y el cash maximo por mercado fue 24,60 USDC.

El coste ponderado del set fue 1,00045253 frente al maximo congelado de 0,99. Este fue el unico fallo de reproduccion de comportamiento.

## Rendimiento paper

- Capital desplegado acumulado entre mercados: 5.821,04325 USDC.
- PnL neto: -262,86825 USDC.
- ROI sobre coste: -4,5158%.
- Primera mitad: -143,06825 USDC.
- Segunda mitad: -119,80 USDC.
- Mercados positivos / negativos / planos: 95 / 191 / 1.
- Max drawdown: 264,26825 USDC.
- Mayor mercado positivo como fraccion del gross positive: 2,716%; no hubo dependencia de un unico outlier.

Los gates de PnL, ROI y ambas mitades fallaron.

## Descomposicion causal

La parte pareada desplego 5.235,54317 USDC y perdio solo 2,36817 USDC, un ROI de -0,0452%. El residual desplego 585,50008 USDC y perdio 260,50008 USDC, un ROI de -44,49%. Por tanto, casi toda la perdida procedio del residual direccional.

Hubo residual en 237 mercados y gano 65, un 27,43%. Cuando el residual coincidio con el favorito inicial, gano 63/171 (36,84%) y perdio 151,63341 USDC. Cuando termino en el lado contrario, gano 2/66 (3,03%) y perdio 108,86667 USDC.

El favorito inicial gano 60,28% de todos los mercados, pero esa cifra no era ejecutable directamente: condicionar la posicion a que la quote residual recibiera fill selecciono casos donde el precio se habia movido en contra. Este es un caso claro de adverse selection maker.

## Veredicto

V0.18 queda rechazado y no habilita dinero real. El analisis de la wallet de X si sirvio para descubrir la acumulacion multifill, pero la regla direccional simplificada y el emparejamiento por promedios no reprodujeron su posible edge.

## Siguiente paso recomendado

V0.19 debe ser una prueba distinta, paper-only y de maximo 24 horas:

1. residual direccional fijado en cero;
2. libro FIFO de lotes no pareados;
3. precio maximo de hedge igual a 0,99 menos el coste real del lote pendiente;
4. prohibicion de emparejar fills de ciclos distintos si el set resultante supera 0,99;
5. maximo 5 shares temporalmente no pareadas;
6. una sola configuracion congelada y nueva ventana live de 24 horas;
7. dinero real bloqueado.

La finalidad de V0.19 sera comprobar si la parte casi neutral de V0.18 puede convertirse en sets rentables por construccion, aceptando que la frecuencia probablemente disminuya.

## Evidencia congelada

- Resultado: `data/resultado_v018_multifill.json` (`49ce6db0feb156f3a066dd1f2abad615c52e33968266b9a51b4b768b01e78387`).
- Cache Gamma: `data/v018_resolution_cache.json` (`c1c2fa97e15eb1f8b753433161bc93c2f2434ff474ae4cabf020d78fc78f712a`).
- Prerregistro de evaluacion: `data/prereg_v018_evaluation.json` (`87992218a045ef2c1177311a3ba03ba2a46ffedbd1811a1983af9fafdc1df379`).
- Base paper final: `data/paper_v018_multifill.db` (`7e325975b09bb29d3891ebad068559fab1ba88f02b401f14ac8db3f2c8ed2fe4`).
