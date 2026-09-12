# V0.28: implementación del runner y auditor

Fecha local: 2026-08-22

## Estado

- Diseño y preinscripción: congelados e inalterados.
- Runner, collector, monitor y auditor: construidos y probados.
- Lanzamiento: `NOT_LAUNCHED`.
- Aprobación de lanzamiento: ausente.
- Base V0.28: ausente.
- Resultado V0.28: ausente.
- Dinero real: `BLOQUEADO`.

La construcción de la infraestructura no concede permiso para iniciar la prueba. El
runner exige un archivo de aprobación independiente, ligado por hash a la
preinscripción y al manifiesto de implementación.

## Archivos de implementación

- `src/polymarket_bot/v028_forward.py`: captura independiente y base SQLite V0.28.
- `src/polymarket_bot/v028_runner.py`: hashes, bloqueo de lanzamiento, checkpoints y
  futilidad.
- `src/polymarket_bot/v028_audit.py`: auditoría terminal de solo lectura.
- `v028_monitor.py`: comandos de estado, construcción, ejecución y auditoría.
- `tests/test_v028_runner_audit.py`: pruebas de contrato, seguridad y auditoría.

## Contrato de datos implementado

- Solo se suscribe el TWAP oficial de 60 segundos.
- Un contrato de resolución de 30 segundos se rechaza como inelegible.
- No existe fallback a otra ventana.
- El TWAP de apertura y el de decisión deben tener una antigüedad máxima de 5
  segundos.
- La distancia se calcula únicamente como diferencia entre ambos TWAP oficiales.
- `abs(twap_distance_to_open_bps) < 5` se aplica con límite estricto.
- El modelo transferido de 30 segundos permanece desactivado.
- La base V0.28 tiene tablas, metadatos, ruta y hashes propios; no reutiliza filas de
  V0.27.

## Runner y checkpoints

Los checkpoints congelados son 4, 8, 12, 16 y 20 horas. No pueden declarar éxito
anticipado. Solo pueden:

- continuar;
- reintentar una comprobación técnica una vez después de 10 minutos;
- congelar por seguridad o fallo técnico;
- congelar la hipótesis primaria cuando la frecuencia o el límite superior de
  rentabilidad indiquen futilidad.

El estado público muestra cantidades y decisiones, pero no PnL ni resultados por
operación durante la ejecución.

## Auditor terminal

El auditor abre la base con `query_only`, comprueba su hash antes y después y genera
como máximo un resultado idempotente para la misma evidencia. Evalúa una sola
hipótesis seleccionable y un control parental no seleccionable.

Puertas implementadas:

- al menos 20 operaciones, con 8 en cada mitad;
- PnL total positivo y profit factor mayor que uno;
- ambas mitades positivas;
- PnL positivo después de retirar la mejor operación;
- al menos cuatro bloques evaluables, dos operaciones por bloque y 60% de bloques
  positivos;
- media de la hipótesis primaria superior a la del control parental;
- límite inferior unilateral 95% de la media estrictamente positivo.

Los únicos veredictos terminales son los seis preinscritos. Incluso un resultado
positivo solo produce un candidato paper; nunca habilita dinero real.

## Seguridad verificada

- `orders_enabled=false`
- `paper_orders_enabled=false`
- `wallet_required=false`
- `real_money=BLOQUEADO`
- duración máxima: 24 horas
- horas nuevas de backtest: 0
- ejecución bloqueada sin aprobación separada
- una alteración del manifiesto de seguridad falla de forma cerrada

## Pruebas

- Pruebas V0.28: 11/11 correctas.
- Suite completa: 272/272 correctas.
- Se validaron el rechazo de TWAP30, la frescura de TWAP60, los límites estrictos,
  la concentración en la mejor operación, la futilidad, el bloqueo sin aprobación,
  la cancelación terminal esperada y la inmutabilidad de la base durante la
  auditoría.

## Comando seguro de consulta

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe v028_monitor.py --status
```

No debe utilizarse `--run` hasta que exista una autorización explícita posterior y
se haya creado el manifiesto de aprobación ligado a los hashes definitivos.
