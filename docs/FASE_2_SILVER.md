# Fase 2 — Dataset Silver por segundo

## Objetivo

Convertir las capturas raw V3/V4 en datos tabulares compactos, reproducibles y
aptos para estadística. Las bases originales se abren con SQLite `mode=ro` y
nunca se actualizan.

## Resolución

Cada slug `btc-updown-5m-<epoch>` define el comienzo exacto del intervalo. El
final es 300 segundos después. Gamma se consulta públicamente por slug y la
etiqueta se acepta únicamente cuando:

- el mercado está cerrado;
- `outcomePrices` contiene un único resultado con valor igual o superior a
  0,99;
- el resto de resultados tiene valor igual o inferior a 0,01;
- el resultado ganador es `Up` o `Down`.

Se conserva la respuesta Gamma completa. También se guardan la regla, el estado
de resolución y `priceToBeat`. La regla de estos mercados establece `Up` cuando
el precio Chainlink final es mayor o igual al inicial; de lo contrario `Down`.

## Capas

| Capa | Contenido | Mutabilidad |
|---|---|---|
| Bronze | V3/V4, payloads originales comprimidos | inmutable |
| Silver | 300 filas por mercado, una por segundo | regenerable |
| Gold | features y particiones walk-forward | fase posterior |

## Campos Silver

Cada segundo guarda:

- Chainlink y antigüedad de la última actualización;
- Binance directo y antigüedad;
- mejores bid/ask, mid y spread para Up y Down;
- profundidad observada a 1 y 5 centavos desde el mejor precio;
- último trade y volumen negociado;
- conteos de libros, cambios, BBA y trades;
- suma de probabilidades y sobreprecio de ejecución;
- segundos restantes;
- banderas explícitas de datos faltantes o BBA inválido.

Los valores se rellenan hacia adelante solo después de haber sido observados.
No se inventan precios anteriores al inicio de la captura.

## Control por mercado

Además del gate global, cada mercado se evalúa individualmente. Solo se marca
como apto para la futura capa Gold si al menos 95% de sus 300 segundos tiene
Chainlink y BBA válido de ambos outcomes. En V4 también se exige 95% de
cobertura Binance. Los mercados que comenzaron antes que el recolector quedan
conservados en Silver, pero aparecen en `training_excluded_markets` y no se
usarán para entrenar ni evaluar modelos.

Los eventos pueden llegar levemente desordenados entre feeds. El procesador
mantiene cada mercado abierto durante 60 segundos adicionales antes de
consolidarlo. Esta tolerancia se incorporó después de observar dos eventos
tardíos entre aproximadamente 1,4 millones en el piloto V4.

## Reanudación segura

Las respuestas Gamma verificadas se guardan en
`data\gamma_resoluciones_fase2.json`. Si internet falla, la siguiente ejecución
reutiliza las etiquetas ya confirmadas. La base Silver se escribe primero con
extensión `.partial` y solo obtiene el nombre definitivo después de superar
todos los controles. Las bases raw se mantienen siempre en solo lectura.

## Gate aprobado del piloto

El piloto procesa los primeros diez mercados de V3 y V4 por separado. Se
aprueba si:

- ambas fuentes permanecen sin cambios de tamaño;
- SQLite `quick_check` devuelve `ok`;
- las veinte etiquetas son verificadas;
- no existen bloques o JSON malformados;
- al menos 80% de las filas tiene Chainlink y BBA completo;
- V4 tiene Binance directo en al menos 80% de sus filas.

V3 no exige Binance porque esa limitación fue precisamente la causa de crear
la captura V4.

## Exclusiones

Esta fase aún no calcula señales, probabilidades del modelo, Kelly, PnL ni
órdenes. Tampoco optimiza parámetros. Primero se valida la calidad tabular y
después se construye Gold con separación temporal estricta.
