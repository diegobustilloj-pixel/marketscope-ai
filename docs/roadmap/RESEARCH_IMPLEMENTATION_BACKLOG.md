# Backlog de investigación a construcción

**Estado de corte:** 11 de septiembre de 2026  
**Regla:** las conclusiones de investigación están cerradas; el estado de cada construcción se actualiza con evidencia verificable.
**Seguridad:** todos los entregables nacen en modo de solo lectura, backtest o shadow. No se habilitan dinero real, firma ni órdenes automáticas.

## Registro acordado

| # | Investigación concluida | Construcción pendiente | Estado |
|---:|---|---|---|
| 1 | Perfil Balthazar | Copiado por reconstrucción | Pendiente |
| 2 | Perfil `car` | Shadow Copy | Pendiente |
| 3 | Perfil `e46m3` | Reconstrucción del mecanismo NegRisk | Pendiente |
| 4 | Perfil `Oxp3mny` | PolyLedger OPS Sentinel: API/WS, relayer, reglas, UMA y capital recuperable | Pendiente; falta fijar perfil y wallet exactos en un artefacto local |
| 5 | Forensia de GitHub | Construir el P0 de PolyLedger | Núcleo implementado; salida P0 bloqueada por evidencia |

`e46m9` se normaliza como `e46m3`, porque ese es el perfil ya auditado y registrado. `PO` se interpreta como **P0**, la prioridad cero definida por la forensia de GitHub. Si alguno de esos dos nombres representa otra entidad, debe corregirse antes de construir.

## Orden profesional de construcción

La numeración anterior conserva el orden de las investigaciones, pero no es el orden técnico recomendado:

```text
P0 PolyLedger
    ├── PolyLedger OPS Sentinel
    ├── Reconstrucción NegRisk (e46m3)
    └── Motor común de copia
            ├── Balthazar: copiado por reconstrucción
            └── car: Shadow Copy
```

### Etapa 1 — P0 de PolyLedger

**Actualización de construcción (2026-09-12):** raw/cursor/reorg, registro y
verificación de contratos, decodificadores CLOB/CTF/NegRisk, motor de lotes,
reconciliación y captura/replay están implementados. PositionManager Combo ya
permite transferencias e IDs estructurales con implementación/fuente verificadas;
faltan la economía y vectores de sus módulos, mapeos de transacciones mixtas y el
replay real de siete días con PnL independiente. El archivo de `car` fue censado
y rechazado como bundle contable incompleto. No se cumple aún el criterio de
salida. Detalle: `docs/architecture/POLYLEDGER_P0.md` y
`docs/operations/POLYLEDGER_P0_RUNBOOK.md`. Las investigaciones siguen cerradas.

Objetivo: crear una única fuente de verdad antes de evaluar o copiar estrategias.

Entregables mínimos:

- registro de contratos, proxies, implementaciones, ABI y rangos de bloques;
- almacenamiento raw inmutable y cursor transaccional reorg-safe;
- decodificadores separados para CLOB V1, CLOB V2/CTF, NegRisk y Combo;
- ledger por lotes con procedencia, costo y PnL realizado/no realizado;
- reconciliación CLOB + ledger + balances onchain;
- control de gaps REST/WS, reorgs, cambios de contrato y fallos de decodificación;
- importes en unidades atómicas o `Decimal`, nunca `float` contable;
- replay determinista de siete días con hashes reproducibles.

Criterio de salida: dos pipelines independientes deben producir los mismos balances y PnL para una wallet conocida, incluyendo pruebas inyectadas de crash, gap y reorg.

### Etapa 2 — PolyLedger OPS Sentinel de `Oxp3mny`

Objetivo: transformar los patrones operativos observados en un centinela explicable.

Componentes:

- salud y reconciliación de API REST y WebSocket;
- observación de relayer, estados, nonces, fallos y reintentos, sin firmar;
- sentinel de reglas por contrato, método, mercado, exposición y estado;
- monitor UMA de propuesta, disputa, resolución y ventanas temporales;
- cálculo de capital recuperable, bloqueado, redimible y no explicado;
- alertas con evidencia y severidad, sin acciones automáticas.

Criterio de entrada: guardar la URL exacta del perfil y la wallet pública de `Oxp3mny` para que la conclusión tenga trazabilidad local.

### Etapa 3 — Reconstrucción NegRisk de `e46m3`

Objetivo: explicar el mecanismo económico, no imitar transacciones aisladas.

Debe reconstruir `split`, `merge`, `convert`, transferencias y `redeem`; enlazar cada cambio de inventario a su transacción; calcular el costo efectivo después de conversiones; y distinguir retorno aparente de beneficio realizado.

Criterio de salida: el replay de muestras cerradas debe conciliar inventario, colateral y redenciones sin residuales no explicados por encima de la tolerancia registrada.

### Etapa 4 — Copiado por reconstrucción de Balthazar

Objetivo: reconstruir la exposición económica completa y decidir si aún es replicable.

No se copia un fill de forma ciega. El motor debe considerar cesta, precio actual, latencia, fees, slippage, profundidad, exposición existente y mecanismo de resolución. La primera salida será un ticket manual con explicación; no una orden.

Criterio de salida: backtest event-time y shadow forward con comparación contra no operar, desglose de costes y límites de exposición.

### Etapa 5 — Shadow Copy de `car`

Objetivo: medir si las señales observadas sobreviven al retraso real de copia.

El sistema registra la operación teórica al precio ejecutable del seguidor, fills parciales, capacidad, retraso, resultado y divergencia frente al líder. No envía órdenes.

Criterio de salida: muestra suficiente preregistrada, PnL neto shadow, drawdown, cobertura, tasa de señales descartadas y sensibilidad a latencia/slippage.

## Dependencias y reutilización

- Balthazar y `car` compartirán un solo motor de reconstrucción/copia.
- `e46m3` aportará el decodificador y la contabilidad NegRisk al ledger común.
- OPS Sentinel consumirá la misma adquisición y reconciliación; no mantendrá otra base contable paralela.
- La forensia GitHub es el diseño de infraestructura, no una estrategia rentable.
- Ninguna conclusión de perfil prueba rentabilidad futura ni autoriza copiar capital.

## Evidencia existente

- Balthazar: `C:\ProyectoBotV4\archives\research\balthazar\AUDITORIA_BALTHAZAR_X.md` y `docs/PLAN_COPIADO_POR_RECONSTRUCCION_V001.md`.
- `car`: `data/car_forensics/car_handoff.md` y `data/car_forensics/car_strategy_report.md`.
- `e46m3`: `data/e46m3_forensics/e46m3_handoff.md` y `data/e46m3_forensics/e46m3_strategy_report.md`.
- GitHub: `data/github_forensics_20260911/github_research_handoff.md`, `polymarket_architecture_recommendations.md` y `polymarket_quick_wins.md`.
- `Oxp3mny`: conclusión confirmada por el usuario; falta importar la evidencia y fijar su identificador público exacto.
