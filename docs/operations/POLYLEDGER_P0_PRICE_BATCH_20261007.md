# Lote de captura histórica de precios — PolyLedger P0 / `car` / 2026-10-07

## Alcance

Se añadió un cursor estable a `probe-price-history` para continuar la cola de
3.498 solicitudes sin repetir los primeros 20 outcomes de la sonda original.
Cada lote conserva el mismo bloque de cierre, el mismo corte `as_of`, la misma
fuente oficial y el mismo límite de 20 respuestas. Un lote sólo crea evidencia
de precios; no escribe marcas, no audita identidad por sí solo y no modifica el
bundle v5.

## Lote intentado

| Campo | Valor |
|---|---|
| Rango lógico | índices 20–39, secuencias 21–40 |
| Salida final prevista | `data/polyledger-sentinel/p0/car_lifetime_price_probe_20261007_batch_00021_00040/` |
| RPC primario | `https://polygon.drpc.org` |
| RPC secundario | `https://tenderly.rpc.polygon.community` |
| Fuente de precios | `https://data-api.polymarket.com/v2/prices-history` |
| Frescura usada | 900 segundos |
| Límite del lote | 20 outcomes |

## Resultado

La ejecución del 7 de octubre quedó `BLOCKED` antes de consultar precios:
el transporte del entorno no pudo completar `eth_chainId`. Se conservó el
directorio parcial
`car_lifetime_price_probe_20261007_batch_00021_00040.partial/`, que contiene
`configuration.json` y `failure.json`; no existe salida final, no se guardó
ninguna respuesta de la API y no se modificó el bundle ni ninguna marca.

La causa es de acceso de red del entorno, no evidencia de que el rango esté
vacío. Para reanudar, mantener ese `.partial` y usar un nuevo sufijo de salida,
por ejemplo `_retry1`, cuando estén disponibles los RPC. Después se debe
auditar cada lote con las fuentes CLOB/Gamma y aplicar una política de aceptación
con raíz de confianza nueva; no se puede usar directamente como PnL.

## Código y seguridad

El cursor quedó registrado en el commit `cb3e03d`. Las pruebas específicas del
sondeo, auditoría y política pasan (36 pruebas). El comando sigue siendo sólo
lectura: sin wallet, firma, órdenes, retiros ni dinero real.
