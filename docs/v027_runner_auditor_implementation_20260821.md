# V0.27 — implementación del runner y auditor

Fecha de construcción: 2026-08-21

## Estado

- Variante: `V0.27_REGIME_REPLICATION_TOURNAMENT_24H`.
- Runner, almacenamiento, checkpoints y auditor: construidos.
- Estado operativo: `NOT_STARTED`.
- Lanzamiento: `NOT_LAUNCHED`; falta aprobación explícita.
- Base de datos y resultado final: no existen.
- Dinero real y órdenes paper: bloqueados.
- Wallet: no requerida.
- Backtesting nuevo realizado para esta implementación: 0 horas.

El diseño congelado no se modificó. La implementación está registrada por separado en
`data/implementation_v027_regime_tournament.json`, con hashes del código y de la
preinscripción.

## Contrato de datos

- La ventana TWAP oficial se determina por mercado mediante el contrato de resolución.
- Se admiten 30 y 60 segundos, almacenados sin colisiones aunque compartan timestamp.
- Si la fuente es ausente, ambigua o no admitida, el mercado falla de forma cerrada.
- Para operar una señal se exige TWAP oficial fresco tanto en apertura como en decisión.
- Cuando el mercado usa 60 segundos, se usan exclusivamente los ticks de 60 segundos.
- El modelo transferido entrenado con 30 segundos permanece desactivado.
- La probabilidad para la señal procede del precio implícito del mercado; no se usa un
  modelo transferido como sustituto.

## Estrategias y evaluación

El torneo conserva exactamente los brazos preinscritos:

- Candidato `favorite_up_low_vol_lt_075`: mínimo 15 señales finales y 5/5 por mitad.
- Candidato `favorite_down_cost_070_080`: mínimo 10 señales finales y 3/3 por mitad.
- Los dos controles amplios se capturan para comparación, pero nunca son seleccionables.

Las puertas económicas exigen simultáneamente:

- PnL total positivo.
- Profit factor total y por cada mitad por encima de 1.
- Al menos 2 de 4 bloques de seis horas evaluables.
- Fracción positiva de bloques evaluables de al menos 0,60.
- PnL medio superior al control padre correspondiente.

La puerta estadística aplica Bonferroni unilateral con alfa 0,025 por candidato y
`z = 1.959963984540054`. Como máximo se selecciona un candidato.

## Checkpoints y congelamiento

Los checkpoints están fijados en las horas 4, 8, 12, 16 y 20. La futilidad se evalúa
por candidato, no de forma global. Congelar un candidato no detiene al otro. El
experimento completo sólo se detiene antes de 24 horas si ambos candidatos quedan
congelados o se activa una barrera técnica/de seguridad preinscrita.

El comando de estado no revela outcomes ni PnL durante la captura. La auditoría es de
solo lectura y se ejecuta una vez al concluir.

## Verificación

- Suite completa: 258 pruebas superadas.
- Se comprobó en la interfaz real que `--run` sin el archivo de aprobación falla antes
  de crear la base de datos.
- El hash congelado de `phase41.py` se conserva en
  `8835d4c2a03331f539e66ef9ce6648d71c4c4186b9d0205fbe006c2f876707a0`.

## Comandos seguros para el usuario

Desde CMD:

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe v027_monitor.py --status
```

Después de que un forward autorizado haya terminado, el auditor se ejecutará con:

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe v027_monitor.py --audit
```

No se debe usar `--run` hasta crear una autorización explícita, única y enlazada por
hashes a esta implementación y a la preinscripción congelada.

## Próximo paso seguro

Revisar esta implementación y, sólo si se aprueba expresamente, generar el manifiesto
de autorización y lanzar un único forward paper de hasta 24 horas. No habilitar dinero
real, wallet ni órdenes.
