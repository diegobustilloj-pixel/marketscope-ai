# Decisión V0.31R — calidad de datos y libro unilateral

Fecha: 2026-08-24 (hora de Bolivia)

## Resultado

La reauditoría cerrada V0.31R obtiene:

`PASS_TECHNICAL_CAPTURE_ONLY_REAUDITED_SEMANTICS_V2`.

Este pase es exclusivamente técnico y post-hoc. No reemplaza el resultado
oficial V0.31 `FAIL_TECHNICAL_QUALITY`, no constituye validación independiente
y no permite afirmar rentabilidad.

## Corrección compatible

No se modificaron el collector, el auditor, la base ni el resultado oficial de
V0.31. Se agregó una capa versionada que separa:

- `UNINITIALIZED`: el libro todavía no fue recibido;
- `STALE`: el libro fue recibido, pero está obsoleto;
- `INVALID_LEVELS`: niveles estructuralmente inválidos;
- `EMPTY_OBSERVED`: libro fresco observado sin niveles;
- `BID_ONLY_OBSERVED`: solo existe liquidez para vender;
- `ASK_ONLY_OBSERVED`: solo existe liquidez para comprar;
- `TWO_SIDED_OBSERVED`: compra y venta disponibles.

Una lista bid o ask vacía ya no se interpreta como pérdida de datos cuando el
libro está inicializado, fresco y estructuralmente válido. Sigue bloqueando de
forma fail-closed la acción que requiere esa cara del libro.

## Métricas de calidad

- mercados: 12;
- snapshots: 3.589;
- completos con semántica original: 2.829, `78,824185 %`;
- completos con semántica V2: 3.536, `98,523266 %`;
- filas reclasificadas correctamente: 707;
- filas completas originales que empeoraron bajo V2: 0;
- libros no inicializados: 24 por outcome;
- libros obsoletos: 14 por outcome;
- niveles estructuralmente inválidos: 0;
- SQLite `quick_check`: `ok`;
- hash de la base idéntico antes y después;
- hash del resultado oficial idéntico antes y después.

Todas las puertas técnicas V2 y todas las puertas originales de seguridad
pasaron.

## Mecánica de ejecución observada

- disponibilidad simultánea de las cuatro acciones: 2.844/3.589,
  `79,242129 %`;
- exactamente una sola pata comprable: 707/3.589, `19,699081 %`;
- exactamente una sola pata vendible: 707/3.589, `19,699081 %`;
- máximo tramo unilateral continuo: 146 segundos;
- disponibilidad individual:
  - comprar Up: `90,080802 %`;
  - vender Up: `88,102536 %`;
  - comprar Down: `88,102536 %`;
  - vender Down: `90,080802 %`.

La disponibilidad simultánea de compra y venta se deteriora al acercarse la
resolución:

- segundos 0–29: 325/349;
- 30–89: 720/720;
- 90–149: 720/720;
- 150–179: 322/360;
- 180–209: 280/360;
- 210–239: 248/360;
- 240–269: 171/360;
- 270–299: 58/360.

Esta muestra no justifica elegir post-hoc un segundo exacto de entrada o salida.
Sí demuestra que una estrategia que suponga ejecución simétrica hasta el cierre
es técnicamente inválida.

## Requisitos obligatorios para el siguiente candidato

1. Verificar la cara y profundidad necesarias de cada pata en el instante de la
   decisión y nuevamente con la latencia preinscrita.
2. Bloquear una compra si no existe ask y bloquear una venta si no existe bid.
3. No fabricar liquidez usando el complemento `1 - precio`.
4. No asumir atomicidad entre las dos órdenes.
5. Definir antes de probar una contingencia explícita para inventario de una
   sola pata.
6. Separar calidad de datos de ejecutabilidad económica.
7. Preinscribir cualquier regla temporal, umbral de profundidad, coste y salida
   antes de leer outcomes o calcular PnL.

## Seguridad

- outcomes leídos: 0;
- labels leídos: 0;
- PnL calculado: no;
- señales generadas: no;
- órdenes: 0;
- paper orders: 0;
- wallet requerida: no;
- dinero real: `BLOQUEADO`;
- siguiente lanzamiento automático: no.

## Artefactos nuevos

- `data/prereg_v031_quality_semantics_reaudit_v2.json`;
- `data/implementation_v031_quality_semantics_reaudit_v2.json`;
- `src/polymarket_bot/v031_quality_reaudit.py`;
- `v031_quality_reaudit_report.py`;
- `tests/test_v031_quality_reaudit.py`;
- `data/resultado_v031_quality_semantics_reaudit_v2.json`;
- `docs/DECISION_V031R_QUALITY_SEMANTICS_20260824.md`.

Pruebas específicas: 6/6. Suite completa: 314/314.
