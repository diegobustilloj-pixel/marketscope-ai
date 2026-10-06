# Plan de fuentes oficiales para cerrar la evidencia P0

**Estado:** diseño de adquisición; no se han importado ni imputado valores con
este plan. La cola local sellada de brechas sigue siendo la única lista de
trabajo autorizada.

## Fuentes candidatas y límites

| Necesidad | Fuente oficial candidata | Uso permitido | Límite que se conserva |
|---|---|---|---|
| Marcas históricas por outcome | [`/v2/prices-history`](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history) | Última observación publicada en o antes del corte, con timestamp y resolución conservados | Una serie puede ser escasa o no existir; no se sustituye con precio actual ni con una interpolación. |
| Consulta eficiente de varias posiciones | [`/batch-prices-history`](https://docs.polymarket.com/api-reference/markets/get-batch-prices-history) | Lotes de hasta 20 asset IDs, solo como transporte de datos | Cada respuesta raw, request y cursor/ventana se sellan; el límite no convierte precios en evidencia suficiente. |
| Contraste de cuenta | [`/v1/accounting/snapshot`](https://docs.polymarket.com/api-reference/misc/download-an-accounting-snapshot-zip-of-csvs) | ZIP oficial de `positions.csv` y `equity.csv` para la wallet pública | Es un snapshot de cuenta; no prueba por sí solo el PnL histórico ni reemplaza el segundo pipeline. |

Las páginas oficiales describen que la lectura de historial admite `as_of` y
devuelve el último punto observado en o antes de ese instante. También aclaran
que la serie puede ser irregular y que su granularidad disponible depende de
la antigüedad de la consulta. Por eso toda selección debe retener la edad de la
observación y fallar cerrada si supera el umbral acordado.

## Secuencia de captura propuesta

1. Obtener el timestamp del bloque de cierre `93.762.690` y comprobar su hash
   contra una fuente Polygon independiente. Sellar el encabezado raw antes de
   pedir precios.
2. Partir los 3.498 outcomes positivos de la cola en lotes de veinte como
   máximo (hasta 175 solicitudes). Consultar solo ventanas que terminen en el
   corte y guardar bytes raw, URL/cuerpo, hora de recepción, hash y respuesta
   HTTP.
3. Para cada activo, escoger únicamente el último tick con timestamp no
   posterior al corte. Persistir `price`, timestamp de observación,
   `resolution_seconds`, origen y distancia al corte. Si no existe un tick o
   no supera el criterio de frescura, dejar la marca ausente.
4. Para las 856 valoraciones de frontera, resolver primero el timestamp exacto
   de cada transacción del bundle y aplicar la misma regla de observación no
   posterior. Una recepción solo recibe `received_basis` cuando el valor y su
   evidencia coinciden; una transferencia conserva su valor de salida por
   separado.
5. Compilar un bundle nuevo con esos artefactos como evidencia. Nunca editar
   el bundle v5 ni reemplazar la salida v2.
6. Construir el cálculo independiente a partir de una procedencia separada;
   el snapshot de cuenta puede ser contraste de posiciones/equity, no un atajo
   para declarar PnL histórico.

## Reglas de rechazo

- No usar precios actuales para un bloque histórico.
- No redondear `float` silenciosamente: retener el texto/raw de origen y
  convertirlo a `Decimal` bajo una política explícita.
- No suponer que un token sin historial vale cero.
- No aceptar un ZIP actual como sustituto de la evidencia de un bloque pasado.
- No marcar el gate como aprobado por completar una sola de las tres fuentes.

Antes de iniciar una captura masiva se debe añadir un manifest de fuente,
límites de frescura, reintentos/rate-limit y hashes, y ejecutar primero una
sonda acotada contra la cola sellada.
