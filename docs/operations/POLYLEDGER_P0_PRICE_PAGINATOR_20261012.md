# Paginador automático de precios — PolyLedger P0 / `car` / 2026-10-12

Estado de código: **implementado y cubierto por pruebas; sólo lectura, sin
integración de marcas, PnL, wallet, firma, órdenes ni dinero real.**

Validación local: **154 pruebas `test_ledger*` aprobadas y 11 configuraciones de
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

## Salida canónica de esta corrida

```text
data/polyledger-sentinel/p0/
└── car_lifetime_price_pagination_20261012_from_00120_v1/
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
  --output data/polyledger-sentinel/p0/car_lifetime_price_pagination_20261012_from_00120_v1 `
  --rpc-url https://polygon.drpc.org `
  --secondary-rpc-url https://tenderly.rpc.polygon.community `
  --start-index 120 --batch-size 20 --max-age-seconds 900
```

## Estado sin modificar la corrida

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger price-pagination-status `
  --output data/polyledger-sentinel/p0/car_lifetime_price_pagination_20261012_from_00120_v1
```

## Reanudación

Después de una pausa o interrupción:

```powershell
.\.venv\Scripts\python.exe -m polymarket_bot.ledger paginate-price-history `
  --bundle data/polyledger-sentinel/p0/car_lifetime_basis_bundle_20260915_v5/bundle.json `
  --evidence-gaps data/polyledger-sentinel/p0/car_lifetime_basis_evidence_gaps_20261006_v1 `
  --metadata-db data/car_forensics/car_metadata.db `
  --policy-template configs/polyledger/closing_mark_paginator_template_v1.json `
  --output data/polyledger-sentinel/p0/car_lifetime_price_pagination_20261012_from_00120_v1 `
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
