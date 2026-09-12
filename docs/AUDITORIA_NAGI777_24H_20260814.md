# Auditoría pública de nagi777 — ventana congelada de 24 horas

Fecha de captura: 2026-08-14. Veredicto: `PARTIAL_DESCRIPTIVE_MATCH / INSUFFICIENT_EXECUTION_EVIDENCE`.

## Resultado ejecutivo

La wallet pública de `nagi777` quedó identificada con coincidencia exacta en dos fuentes oficiales independientes de Polymarket:

- username: `nagi777`;
- proxy wallet: `0xbf337426aa856996b8bb79b238345dd1a0276bf7`;
- pseudónimo: `Alarmed-Hide`;
- X asociado: `Nagi__777__`.

La actividad pública respalda una parte importante de la descripción de RetroValix: compra ambos resultados en prácticamente todos los mercados, acumula inventario pareable, conserva un residual direccional y cambia repetidamente entre UP y DOWN. Sin embargo, esta ventana no reproduce el coste promedio de set de 0,9843 publicado y no permite verificar que las ejecuciones procedan de límites simultáneos ni que el edge sea replicable.

Por tanto, la wallet ya no es evidencia faltante, pero la estrategia exacta continúa sin ser reproducible.

## Protocolo congelado

- Ventana UTC: `[2026-08-13 18:40:00, 2026-08-14 18:40:00)`.
- Ventana Bolivia: `[2026-08-13 14:40:00, 2026-08-14 14:40:00)`.
- Duración: exactamente 24 horas.
- Fuente: endpoint público `/activity`, filtrado a `TRADE`.
- Paginación: 96 segmentos no solapados de 15 minutos; 97 páginas totales.
- Filtro congelado: slugs `btc-updown-5m-[epoch de 10 dígitos]`.
- Resoluciones, labels y outcomes ganadores leídos: 0.
- PnL calculado: no.
- Forward activo leído o modificado: no.

El prerregistro fijó la identidad, la ventana, las métricas y el hash del módulo antes de descargar la actividad completa. La primera exploración de 10.000 filas se declaró dentro del prerregistro y se usó únicamente para detectar que el endpoint `/trades` no cubría 24 horas y que era necesario paginar `/activity` con `start/end`.

## Integridad de la captura

- 21.807 registros públicos de actividad.
- 21.807 registros pertenecen a BTC UP/DOWN 5m.
- 276 mercados distintos.
- 19.050 hashes de transacción distintos.
- 0 filas exactamente duplicadas.
- Todos los registros pertenecen a la wallet congelada.
- Todos los registros tienen `type=TRADE` y `side=BUY`.
- Timestamp mínimo: 1786646404.
- Timestamp máximo: 1786732732.
- Solo un segmento alcanzó 500 filas y fue paginado; la página siguiente cerró ese segmento.
- Segunda ejecución: `VERIFIED_EXISTING`; no sobrescribió el snapshot ni el resultado.

## Comparación con el post

| Afirmación | Valor publicado | Proxy observado en 24h | Lectura correcta |
|---|---:|---:|---|
| Compra UP y DOWN | Cualitativa | 275/276 mercados, 99,64% | Coincidencia descriptiva fuerte. |
| Capital pareado | 78,7% | 82,27% estimado | Cercano, diferencia +3,57 puntos; no es contabilidad exacta de posiciones. |
| Residual direccional | 21,3% | 17,73% estimado | Cercano y complementario al proxy pareado. |
| Coste promedio del set | 0,9843 | 1,00572 ponderado por shares | No reproducido en esta ventana. |
| Mediana del coste por mercado | No publicada | 0,99046 | La mitad de mercados queda aproximadamente bajo 1, pero no prueba edge neto. |
| Mercados con coste estimado <1 | Narrativa general | 140/275, 50,91% | Hay sets baratos y caros; p10=0,80751, p90=1,19886. |
| Trades por hora activa | 51,25 | 908,63 registros/h; 762 hashes/hora UTC activa | Las unidades no coinciden; el post no define su agrupación. |
| Average trade | $110,67 | $8,72 por registro público | No comparable sin conocer cómo agrupó fills el autor. |

El promedio simple del coste estimado por mercado fue 0,99978, mientras el promedio ponderado por shares fue 1,00572. Ninguno coincide con 0,9843. Esto no demuestra que el dato histórico del post sea falso: la ventana, el régimen y la metodología del autor podrían ser distintos. Sí demuestra que no debe trasladarse 0,9843 como supuesto fijo para un bot nuevo.

## Comportamiento reconstruido

- 2.716 cambios observados entre compras consecutivas UP y DOWN.
- Primera ejecución del mercado: mediana alrededor del segundo 9.
- Última ejecución del mercado: mediana alrededor del segundo 243.
- Diferencia entre la primera compra de cada lado: mediana 20 segundos y p90 78 segundos.
- Registros por mercado: mediana 70, p10 38 y p90 131,3.
- Valor total de actividad pública en la ventana: aproximadamente $190.239,18.
- Valor medio por hash de transacción: aproximadamente $9,99.
- Valor medio agregado por mercado-segundo: aproximadamente $18,16.

Esto describe acumulación densa y escalonada durante casi toda la vida del mercado. V0.16/V0.17 representaban una simplificación de una entrada y un hedge posterior; su resultado negativo sigue siendo válido para esa simplificación, pero no falsifica este patrón real de múltiples compras y cambios de lado.

## Qué no puede saberse con estos datos

- Órdenes límite publicadas pero no llenadas.
- Cancelaciones, replacements y partial fills del ciclo de cada orden.
- Si cada ejecución fue maker o taker con atribución suficiente.
- Prioridad de cola, latencia y distancia al mejor precio.
- Regla que determina precios, tamaños y skew.
- Rebates y coste neto exacto.
- Método con el que RetroValix agrupó ejecuciones en “trade”.
- Rentabilidad de esta ventana, porque el protocolo prohibió outcomes y PnL.

La API pública prueba actividad ejecutada; no reconstruye el libro privado de órdenes del trader.

## Impacto sobre QuantBot

1. No rescatar V0.16 ni V0.17: permanecen `FAIL` para sus reglas congeladas.
2. No copiar el supuesto de coste 0,9843: la ventana contemporánea produjo 1,00572 ponderado.
3. Sustituir la hipótesis “una entrada + un hedge” por una hipótesis separada de acumulación multi-fill con inventario objetivo.
4. Congelar sus reglas antes de cualquier evaluación de rentabilidad.
5. Probar únicamente en paper y durante un máximo de 24 horas.

## Siguiente prueba válida

Diseñar V0.18 como réplica de comportamiento, no como estrategia aprobada:

- cotizaciones paper en ambos lados;
- múltiples tamaños pequeños durante el mercado;
- límite explícito de inventario pareado y residual;
- cancelación/reprice modelados con latencia y profundidad;
- ninguna orden real;
- ventana nueva máxima de 24 horas;
- gates de ejecución y estabilidad congelados antes de observar PnL.

El resultado actual permite diseñar esa prueba, pero todavía no autoriza llamarla rentable ni activar riesgo.

## Artefactos

- `data/prereg_nagi777_wallet24h_20260814.json`
- `data/nagi777_activity_24h_20260814.json`
- `data/resultado_nagi777_wallet24h_20260814.json`
- `src/polymarket_bot/wallet_window.py`
- `auditar_nagi777_24h.py`
- `tests/test_wallet_window.py`

SHA-256:

- Prerregistro: `9708fffdaa93a92a1910245d8d27f6284dd10a827ec7a6d4312302ab0f3582cf`
- Snapshot: `7690ac4c7718b13c0674bb5959f21c8c0e70a9603d8bb07ce8de69e2ddec9dea`
- Resultado: `d1218a75a69d5f16b42c62555f357ed2e329be3bffe2a5b23e578feeab6a4fa6`

## Fuentes técnicas

- Polymarket Data API: https://docs.polymarket.com/api-reference/introduction
- Leaderboard público: https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings
- Perfil público: https://docs.polymarket.com/api-reference/profiles/get-public-profile-by-wallet-address
- Actividad pública con `start/end`: https://docs.polymarket.com/api-reference/core/get-user-activity
