# Paginador automático de precios — PolyLedger P0 / `car` / 2026-10-12

Estado de código: **implementado y cubierto por pruebas; sólo lectura, sin
integración de marcas, PnL, wallet, firma, órdenes ni dinero real.**

Validación local: **156 pruebas `test_ledger*` aprobadas y 11 configuraciones de
bots válidas, sin errores ni advertencias.** Las cinco pruebas propias cubren
pausa/reanudación, reintento explícito, cierre contiguo, confinamiento de rutas,
último lote parcial y bloqueo sin salto después de reintentos.

## Objetivo

Continuar la cola CTF desde el cursor `120` hasta el final de los 2.870 outcomes
elegibles sin ejecutar manualmente 138 lotes. El orquestador reutiliza los tres
motores sellados existentes:

1. `probe-price-history` para capturar hasta 20 respuestas raw;
2. `audit-price-candidates` sólo cuando el lote contiene candidatos frescos;
3. `evaluate-price-policy` con una raíz de confianza nueva por lote.

Un lote sin candidatos frescos queda cerrado por su sonda sellada; no se hacen
consultas de identidad ni estado CTF porque no existe una marca candidata que
evaluar.

## Garantías operativas

- El cursor sólo avanza después de cerrar un `batch_result.json` verificable.
- Cada reintento usa `attempt_NNN`; nunca sobrescribe el intento anterior.
- Un error de transporte, parseo, identidad, discrepancia RPC o cambio de input
  bloquea el lote; nunca se lo omite.
- El estado se actualiza de forma atómica y un lock de proceso impide dos
  escritores simultáneos.
- Al reanudar se recalculan los acumulados desde los resultados sellados y se
  verifican otra vez los hashes de sonda, auditoría y política.
- La base SQLite de metadata exige un WAL vacío al iniciar y se copia byte por
  byte a un snapshot inmutable dentro de la corrida; todos los lotes leen esa
  copia, no una base que pueda seguir cambiando.
- El bundle, su manifest, la cola de brechas, la base de metadata y la plantilla
  deben conservar exactamente los hashes iniciales.
- La plantilla mantiene `allow_negrisk=false`, dos RPC independientes y cero
  permisos de integración o ejecución.
- Los archivos raw usan claves acotadas; el token y la consulta continúan
  ligados criptográficamente al registro, sin introducir el uint256 completo
  en nombres que puedan superar el límite de rutas de Windows.

## Primer smoke y corrección de ruta

El smoke inicial del 13 de octubre en la salida descriptiva larga `v1` quedó
`BLOCKED` tras tres intentos, todavía en el cursor `120`: Windows rechazó una
ruta de 272 caracteres al guardar el primer raw. No hubo ningún lote completo,
marca integrada ni modificación del bundle. Esa salida se conserva localmente
como evidencia fallida. El motor corrigió los nombres raw sin cambiar su
contenido ni trazabilidad y la corrida canónica continúa en la ruta corta `v2`.

## Corte seguro en 1540 y segundo segmento

`car_px120_v2` cerró 71 lotes contiguos, procesó 1.420 outcomes y llegó al
cursor `1540`. Sus decisiones acumuladas son 145 aceptadas sólo para una futura
compilación, 5 diferidas y 76 rechazadas. El lote 1541–1560 se bloqueó en sus
tres intentos porque el auditor exigía etiquetas literales Yes/No a un mercado
binario Anthropic/OpenAI. Los raw oficiales muestran que CLOB y Gamma sí
coinciden exactamente en etiquetas, condición y par de tokens.

El auditor corregido exige exactamente dos etiquetas no vacías y conserva todas
las demás comprobaciones. Los raw reales bloqueados se reprodujeron localmente
como `ANTHROPIC/OPENAI`. Como el commit forma parte de la raíz de confianza, v2
no se reescribe ni reanuda con código nuevo: la continuación comienza en
`car_px1540_v3`.

Inicio acotado del segundo segmento:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger paginate-price-history `
  --bundle data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --metadata-db data/car_forensics/car_metadata.db `
  --policy-template configs/polyledger/closing_mark_paginator_template_v1.json `
  --output data/polyledger-sentinel/p0/car_px1540_v3 `
  --rpc-url https://polygon.drpc.org `
  --secondary-rpc-url https://tenderly.rpc.polygon.community `
  --start-index 1540 --batch-size 20 --max-age-seconds 900 --max-batches 1
```

Reanudación después del smoke:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger paginate-price-history `
  --bundle data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --metadata-db data/car_forensics/car_metadata.db `
  --policy-template configs/polyledger/closing_mark_paginator_template_v1.json `
  --output data/polyledger-sentinel/p0/car_px1540_v3 `
  --rpc-url https://polygon.drpc.org `
  --secondary-rpc-url https://tenderly.rpc.polygon.community `
  --start-index 1540 --batch-size 20 --max-age-seconds 900 --resume
```

## Salida canónica de esta corrida

```text
data/polyledger-sentinel/p0/
└── car_px120_v2/
    ├── configuration.json
    ├── state.json
    ├── inputs/
    │   └── metadata_snapshot.db
    ├── batches/
    │   └── batch_00121_00140/
    │       └── attempt_001/
    │           ├── probe/
    │           ├── audit/       # sólo si hubo candidatos frescos
    │           ├── policy.json  # sólo si hubo candidatos frescos
    │           ├── policy/      # sólo si hubo candidatos frescos
    │           └── batch_result.json
    ├── summary.json             # sólo al completar el rango
    └── run_manifest.json        # sólo al completar el rango
```

Los datos permanecen locales e ignorados por Git. Git conserva el motor, la
plantilla, las pruebas y esta guía.

## Inicio

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger paginate-price-history `
  --bundle data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --metadata-db data/car_forensics/car_metadata.db `
  --policy-template configs/polyledger/closing_mark_paginator_template_v1.json `
  --output data/polyledger-sentinel/p0/car_px120_v2 `
  --rpc-url https://polygon.drpc.org `
  --secondary-rpc-url https://tenderly.rpc.polygon.community `
  --start-index 120 --batch-size 20 --max-age-seconds 900
```

## Estado sin modificar la corrida

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger price-pagination-status `
  --output data/polyledger-sentinel/p0/car_px120_v2
```

## Reanudación

Después de una pausa o interrupción:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger paginate-price-history `
  --bundle data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --metadata-db data/car_forensics/car_metadata.db `
  --policy-template configs/polyledger/closing_mark_paginator_template_v1.json `
  --output data/polyledger-sentinel/p0/car_px120_v2 `
  --rpc-url https://polygon.drpc.org `
  --secondary-rpc-url https://tenderly.rpc.polygon.community `
  --start-index 120 --batch-size 20 --max-age-seconds 900 --resume
```

Si el estado es `BLOCKED`, primero se inspecciona el intento fallido. Sólo si
la causa fue transitoria se añade `--retry-blocked`; el cursor no cambia durante
ese reintento.

## Interpretación

`COMPLETE` significa que el rango fue recorrido y la evidencia quedó sellada.
No significa que P0 esté aprobado. Las decisiones aceptadas siguen siendo sólo
candidatas para compilar un bundle nuevo; además continúan pendientes los 856
flujos externos, el basis de 772 recepciones y el informe independiente.
