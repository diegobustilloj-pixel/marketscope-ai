# Fase 4.1 — Shadow forward testing ligero

## Propósito

La validación histórica encontró señales prometedoras a 60 segundos, pero
ninguna superó el límite de confianza ajustado. Esta fase registra decisiones
futuras completamente fuera de muestra, sin abrir los 177 mercados del test
histórico y sin crear órdenes.

Las tres hipótesis quedan congeladas antes de comenzar:

| Modelo | Horizonte | Edge mínimo | Universo |
|---|---:|---:|---|
| Logística combinada | 60 s | 0,06 | Todos los mercados |
| Boosting combinado | 60 s | 0,03 | Todos los mercados |
| Boosting con strike | 60 s | 0,10 | Solo strike oficial |

No se cambian modelos, variables ni umbrales durante los siete días.

## Datos y almacenamiento

Chainlink, Binance y el CLOB se procesan en memoria. La base permanente guarda:

- identificación y horario del mercado;
- una única fila de características a 60 segundos;
- probabilidad y señal de cada modelo;
- ask, coste, comisión, slippage y edge;
- resolución pública verificada;
- salud de conexiones y contadores acumulados.

No guarda cada mensaje WebSocket ni el payload crudo. La protección detiene el
proceso si la base alcanza 1 GB o quedan menos de 20 GB libres.

## Reanudación

El inicio del experimento y su fecha objetivo se escriben una sola vez. Si
Windows reinicia o la ventana se cierra, ejecutar nuevamente
`iniciar_shadow_forward_7_dias.bat` continúa sobre la misma base. El tiempo
durante el cual el equipo estuvo apagado cuenta como falta de cobertura y no se
rellena artificialmente.

## Gate final

La auditoría exige:

- 168 horas completadas;
- al menos 90% de mercados esperados;
- al menos 85% con características válidas;
- al menos 90% con resolución verificada;
- base SQLite íntegra y menor a 1 GB;
- test histórico todavía bloqueado;
- Fase 4 sin modificaciones.

Cada estrategia necesita además:

- al menos 100 trades forward;
- límite inferior del PnL medio mayor que cero;
- Brier no más de 0,01 peor que Polymarket.

El límite de confianza aplica Bonferroni a las tres hipótesis preinscritas.

## Seguridad

- `orders_enabled=false`;
- no hay wallet, clave privada, seed phrase ni API privada;
- no usa Kelly ni capital virtual compuesto;
- no utiliza Claude, otro LLM ni servicios de pago;
- la pantalla puede apagarse, pero el equipo no debe suspenderse.
