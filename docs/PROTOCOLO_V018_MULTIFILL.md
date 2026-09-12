# V0.18: maker multifill paper

V0.18 es un experimento independiente de 24 horas para comprobar si una simulacion maker conservadora puede reproducir el comportamiento descriptivo observado en la wallet publica `nagi777` sin atribuirle mecanismos no observables.

## Alcance congelado

- Solo mercados `btc-updown-5m` y solo datos publicos del CLOB.
- Una unica configuracion, sin grid, rescate de umbrales ni cambios durante la corrida.
- Cotizaciones paper de 5 shares entre los segundos 10 y 240, revaluadas cada 10 segundos.
- Latencia minima de activacion de 1 segundo y vida de cada cotizacion de 10 segundos.
- Un fill exige trade-through que limpie el 50% de la profundidad visible delante, o un cruce observado del ask.
- Maximo 25 USDC por mercado, 25 shares por lado y 5 shares de desequilibrio.
- Maker fee y rebate fijados en cero. No se acredita rebate hipotetico.

## Integridad y reanudacion

El prerregistro verifica los hashes del motor, runner y punto de entrada antes de crear o reanudar la base. Los segundos que ya pasaron nunca se reconstruyen. Un mercado interrumpido queda excluido. El bloqueo de proceso impide dos monitores simultaneos; tras un corte electrico, el mismo comando reanuda la base y conserva la hora final original.

## Orden de decision

Primero se comprueba seguridad e integridad tecnica. Luego frecuencia y reproduccion del comportamiento. Los outcomes solo se pueden consultar despues de que la base registre la finalizacion oficial; entonces se aplica una unica evaluacion de rendimiento. El detalle normativo y todos los umbrales viven en `data/prereg_v018_multifill_paper.json`.

## Seguridad

No usa wallet, no firma ni envia ordenes, no toca el forward principal y no lee sus datos. El dinero real permanece bloqueado.
