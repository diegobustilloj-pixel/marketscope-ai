# Fase 3 — Dataset Gold sin fuga temporal

## Objetivo

Transformar los 885 mercados Silver aptos en observaciones reproducibles para
modelado. Esta fase no entrena, optimiza ni ejecuta ninguna estrategia. Primero
se valida que las variables y particiones temporales sean correctas.

## Corrección Gold v2

Gold v1 exigía `priceToBeat` y, por ello, conservó solo 537 mercados. La
auditoría real permitió descubrir que Gamma no incluía ese campo en 348
mercados históricos. Gold v2 no fabrica el dato ni descarta el mercado:

- `price_to_beat` y `distance_to_strike_bps` quedan en `NULL`;
- `has_official_strike=0` identifica estos casos;
- los modelos generales usarán las 885 observaciones por horizonte sin esas
  dos variables;
- un modelo enriquecido podrá evaluarse separadamente con los 537 mercados que
  sí poseen strike oficial.

El gate v2 compara además el número de mercados contra los totales elegibles
de Silver. Esto impide que una reducción silenciosa vuelva a ser aprobada.

## Unidad de observación

Cada mercado produce como máximo cuatro filas:

| Horizonte | Momento de observación |
|---|---|
| 90 | 90 segundos antes del cierre |
| 60 | 60 segundos antes del cierre |
| 30 | 30 segundos antes del cierre |
| 15 | 15 segundos antes del cierre |

Cada fila contiene solamente datos con timestamp menor o igual al momento de
decisión. La etiqueta `y_up` procede del resultado Gamma verificado y nunca se
utiliza como variable explicativa.

## Variables

Gold conserva, entre otras:

- distancia Chainlink al `priceToBeat`;
- retornos de 5, 15, 30, 60 y 120 segundos;
- volatilidad realizada y régimen corto/largo;
- persistencia de tendencia;
- estado y probabilidad Markov calculados únicamente con el pasado;
- precio Binance cuando está disponible;
- bid, ask, mid, spread y probabilidad implícita;
- profundidad e imbalance del libro;
- actividad, volumen y conteos de mensajes;
- calidad observada en el segundo de decisión.

La respuesta final `outcomePrices` de Gamma no entra en las features. Solo se
usa para construir el objetivo binario después del cierre.

## División temporal

Los mercados se ordenan por hora de inicio y se dividen una sola vez:

- 60% inicial: `train`;
- 20% siguiente: `validation`;
- 20% final: `test`.

La división se realiza por mercado antes de crear los cuatro horizontes. Las
filas de un mismo mercado nunca pueden aparecer en particiones diferentes. No
se barajan los datos.

## Binance

V3 no contiene Binance directo y V4 sí. Por ello, las variables Binance se
guardan como opcionales. El primer modelo comparable usará un conjunto `core`
disponible en toda la historia; los modelos con Binance se evaluarán cuando
exista una captura forward considerablemente mayor que las dos horas actuales.

## Gate de aprobación

Gold se aprueba únicamente si:

- ambas bases Silver pasan `quick_check`;
- las bases Silver conservan exactamente su tamaño;
- Gold contiene exactamente todos los mercados declarados aptos por Silver;
- se generan al menos 95% de las filas previstas;
- existen train, validation y test;
- ninguna decisión se ubica fuera del intervalo del mercado;
- no existen features huérfanas;
- todos los mercados elegibles conservan al menos una observación;
- la base Gold pasa `quick_check`.

La salida se construye como `.partial` y recibe el nombre definitivo solo al
superar el gate.

## Siguiente etapa

Después de aprobar Gold se construirán baselines y modelos probabilísticos. La
comparación será temporal y fuera de muestra, e incluirá calibración antes de
simular EV, costes, slippage y Kelly fraccional.
