# Fase 4.2 — Correcciones de infraestructura y diagnóstico

## Motivo

La ventana forward de Fase 4.1 completó 175,2 horas y demostró que la
infraestructura general funcionaba, pero los modelos combinados no mostraron
ventaja rentable. El modelo con strike no pudo evaluarse porque los 1.860
mercados quedaron sin `priceToBeat` oficial.

Los datos de Fase 4.1 ya fueron observados. Se conservan exclusivamente para
diagnóstico y no vuelven a considerarse muestra virgen de validación.

## Cambios de Fase 4.2

### 1. Strike oficial

El discovery shadow consulta primero el endpoint Gamma de Event por slug. El
parser admite `eventMetadata` tanto en el Event como anidado dentro del Market.
Si el Event no proporciona un strike utilizable, existe fallback al endpoint
de Market.

Se registran de forma auditable:

- `event_id`;
- `price_to_beat`;
- `has_official_strike`;
- `final_price` cuando Gamma lo publica;
- `strike_source`;
- `strike_fetch_status`;
- una copia compacta de `eventMetadata` en `event_metadata_json`.

Estados posibles del strike:

- `FOUND`;
- `MISSING_EVENT_METADATA`;
- `MISSING_PRICE_TO_BEAT`;
- `INVALID_PRICE_TO_BEAT`.

Si el strike todavía no existe al descubrir el mercado, se vuelve a consultar
antes de cada checkpoint de decisión hasta encontrarlo. Nunca se inventa ni se
rellena retrospectivamente para producir una señal que no existía en tiempo
real.

### 2. Timeouts de Gamma

`TimeoutError`, `URLError` y errores de red equivalentes se convierten en
`DiscoveryError` recuperable. Un timeout aislado deja de abortar toda la
ventana forward y entra en el mecanismo normal de reintentos.

La misma protección se añadió al discovery general del proyecto.

### 3. Recuperación después de interrupciones

Al abrir una base Fase 4.2:

- cualquier run antiguo con estado `RUNNING` pasa a `INTERRUPTED`;
- se escribe `finished_at`;
- si no había error se registra `RECOVERED_STALE_RUNNING`;
- cualquier mercado ya iniciado cuya feature oficial quedó `PENDING` pasa a
  `FAILED_INTERRUPTED`.

No se intenta reconstruir artificialmente una feature completa después de un
corte, porque se habrían perdido observaciones en memoria.

### 4. Horizontes diagnósticos

La señal oficial congelada continúa exactamente a 60 segundos.

Además se guardan features diagnósticas, sin generar operaciones, a:

- 120 s;
- 30 s;
- 15 s.

Estas filas viven en `shadow_diagnostics` y no participan en los gates de la
estrategia de 60 segundos. Su objetivo es permitir un análisis posterior del
comportamiento conforme se acerca el cierre sin alterar la hipótesis original.

### 5. Separación obligatoria de Fase 4.1

El esquema shadow pasa a versión 2. Fase 4.2 se niega a abrir una base shadow
con esquema 1 para ejecución/reanudación y muestra un mensaje explícito
indicando que debe usarse una base nueva.

Esto protege `shadow_forward_fase41.db` de modificaciones accidentales.

## Seguridad sin cambios

Fase 4.2 mantiene:

- `orders_enabled=false`;
- sin wallet;
- sin claves privadas;
- sin seed phrase;
- sin Kelly;
- sin Claude/LLM tomando decisiones;
- sin creación, firma o envío de órdenes.

Los thresholds congelados siguen siendo:

| Modelo | Edge mínimo | Universo |
|---|---:|---|
| Regresión logística combinada | 0,06 | Todos |
| HistGradientBoosting combinado | 0,03 | Todos |
| HistGradientBoosting strike | 0,10 | Solo strike oficial |

## Pruebas automatizadas

La suite pasó de 30 pruebas a 38 pruebas, además de 3 subtests. Se añadieron
coberturas específicas para:

- `eventMetadata.priceToBeat` en Event;
- strike anidado en Market;
- strike inválido;
- discovery Event-first;
- timeout recuperable;
- recuperación de run `RUNNING`;
- recuperación de feature `PENDING` tras interrupción;
- separación de features diagnósticas y feature oficial.

## Siguiente gate

No iniciar todavía otra ventana de siete días solamente porque el código
compile. Primero ejecutar una prueba técnica corta en una base nueva y comprobar
como mínimo:

1. `sqlite_quick_check = ok`;
2. ningún run stale `RUNNING`;
3. `markets_with_official_strike > 0`;
4. presencia de diagnósticos 120/30/15 s;
5. cero órdenes y cero wallet;
6. continuidad después de simular una interrupción/reinicio.

La prueba corta valida infraestructura, no rentabilidad. Solo después se puede
iniciar una nueva ventana forward independiente para evaluar modelos.


## Actualización v0.9.1: TWAP 30 s

La prueba RTDS en vivo confirmó el tópico `crypto_prices_twap_thirty` para BTC/USD con mensajes `update` de frecuencia aproximada de 1 Hz y `window_s=30`. El bot ahora persiste ese feed en una tabla separada porque no debe asumirse disponibilidad de replay.

La versión v0.9.1 es una fase de captura y diagnóstico. Los modelos heredados se mantienen congelados para comparación, pero no constituyen validación del nuevo régimen de resolución. El modelo Strike anterior queda deshabilitado y `forward_candidate` permanece bloqueado hasta reentrenar y validar una estrategia específica para TWAP en una muestra independiente.
