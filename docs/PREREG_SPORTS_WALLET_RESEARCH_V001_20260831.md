# Prerregistro — investigación deportiva de cuatro wallets V001

## Corte y objetivo

El universo queda congelado en `2026-08-31T15:46:23Z` (`1788191183`), hora obtenida del endpoint público `/time` del CLOB. Se estudiará toda la actividad pública recuperable hasta ese segundo de las cuatro identidades indicadas por el usuario. No se seleccionarán ejemplos ni se truncarán historiales sin declararlo.

## Identidades congeladas

| Clave | Perfil confirmado | Proxy wallet |
|---|---|---|
| `flaznorp` | `Flaznorp` | `0x821dab0565ebf5b327f51db06223fdcfe01acf16` |
| `trader_b` | `0x5016c48436AB3eFA2Ab54b117d0C08fa1a4a1eEB-1778328420816` | `0x5016c48436ab3efa2ab54b117d0c08fa1a4a1eeb` |
| `nigiri99` | `nigiri99` | `0xdc41c39b95453c943174f369926018f6963bdd7e` |
| `kulijan` | `Kulijan` | `0x66834eefe81eb46b184eda12a4a8720bf809f5f4` |

Cada asociación deberá volver a coincidir exactamente en `public-search` y `public-profile` durante la captura.

## Alcance

- Incluir solo mercados clasificados como deporte mediante `sportsMarketType`, tags, series y catálogo `/sports` de Gamma.
- Identificar y cuantificar esports de forma separada; excluirlos de resultados deportivos.
- Conservar toda actividad recuperada, pero derivar el dataset maestro únicamente de filas `TRADE` con metadatos de mercado resueltos.
- Capturar posiciones actuales y cerradas como fuentes de reconciliación, sin confundir sus agregados con fills.
- Mantener los datos crudos, derivados y auditorías separados.

## Exhaustividad y paginación

`/activity` se consultará en ventanas iniciales de 24 horas, orden ascendente, páginas de 500 y offsets permitidos hasta 5000. Si la última página permitida está llena, la ventana se divide por la mitad y se vuelve a consultar. Un solo segundo que supere la capacidad oficial bloqueará la afirmación de cobertura completa.

Las posiciones actuales usarán páginas de 500 hasta offset 10000. Las posiciones cerradas usarán páginas de 50 hasta offset 100000. El informe distinguirá `api_records`, `processed_records`, duplicados exactos, condiciones sin metadatos y cualquier endpoint que alcance su tope.

## Restricciones de inferencia

- La actividad pública prueba fills, no órdenes no llenadas, cancelaciones, reemplazos ni prioridad de cola.
- `prices-history` permite reconstrucción temporal de precios agregados, no profundidad histórica del libro.
- No se asignarán maker/taker, fees, rebates, slippage de profundidad ni fill probability si la fuente no lo demuestra.
- ROI, drawdown y capital eficiente se calcularán solo con denominadores explícitos y se etiquetarán como exactos, reconciliados o proxies.
- Las reglas de estrategia se formularán antes de evaluar el tramo de prueba de cada walk-forward.

## Seguridad

Investigación de solo lectura. No requiere Phantom, credenciales CLOB ni wallet; no crea órdenes ni transacciones. Dinero real: `BLOQUEADO`.
