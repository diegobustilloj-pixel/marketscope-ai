# Compatibilidad TWAP V0.9.5 — 2026-08-21

## Motivo

La evidencia local de mercados BTC Up/Down 5m contiene como fuente oficial
`https://data.chain.link/streams/btc-usd-twap-60s-streams`, mientras que el
forward histórico V0.9.4a1 capturaba y modelaba exclusivamente TWAP de 30 s.
Usar 30 s como sustituto silencioso de 60 s habría cambiado el contrato de
resolución del mercado.

## Implementación compatible

- `phase41.py` permanece byte por byte intacto con SHA-256
  `8835d4c2a03331f539e66ef9ce6648d71c4c4186b9d0205fbe006c2f876707a0`.
  Esto conserva las preinscripciones y auditorías históricas de V0.26b y del
  análisis de 24 horas.
- `resolution_contract.py` extrae únicamente ventanas explícitas de 30 o 60 s.
  Una fuente ausente, genérica, no soportada o ambigua se bloquea; no se infiere.
- El collector RTDS general suscribe Chainlink spot y ambos tópicos TWAP. Los
  eventos continúan usando el formato raw existente.
- `twap_contract_v095.py` usa una base técnica nueva con clave compuesta
  `(source_timestamp_ms, window_s)`, por lo que conserva 30 y 60 s aunque ambas
  actualizaciones tengan la misma marca temporal.
- El modelo transferido entrenado con 30 s sólo es compatible con mercados cuyo
  contrato oficial declare 30 s. Para un mercado de 60 s se bloquea y se exige
  entrenamiento o una estrategia que no dependa de esa variable.

## Captura técnica en vivo

Duración preinscrita por ejecución: 45 segundos. Resultado observado:

- mercado: `btc-updown-5m-1787340000`;
- contrato: `VERIFIED`;
- ventana oficial seleccionada: 60 s;
- actualizaciones únicas 30 s: 40;
- actualizaciones únicas 60 s: 40;
- discordancias tópico/ventana: 0;
- SQLite `quick_check`: `ok`;
- puerta técnica: `PASS`;
- modelo transferido 30 s: `BLOQUEADO_POR_INCOMPATIBILIDAD`;
- reentrenamiento para ventana oficial: requerido;
- órdenes paper: 0; wallet: no requerida; dinero real: bloqueado.

La captura valida transporte, persistencia y selección de contrato. No mide
rentabilidad ni autoriza lanzar V0.27, operar paper o usar dinero real.

Artefactos finales de sólo lectura:

- `data/captura_tecnica_twap_contract_v095.db`;
- `data/resultado_captura_tecnica_twap_contract_v095_final.json`.

## Siguiente paso seguro

Construir el runner y el auditor de V0.27 como archivos nuevos, consumiendo la
ventana oficial seleccionada. Las estrategias que no dependan de TWAP pueden
continuar según su preinscripción; cualquier señal que use distancia TWAP debe
ser entrenada y validada con 60 s antes de incorporarse.
