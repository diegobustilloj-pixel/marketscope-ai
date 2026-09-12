# Post de X e inventario UP/DOWN: pruebas V0.16 y V0.17

Fecha: 14 de agosto de 2026.

## Qué se tomó del post

El post propone una idea útil: analizar UP y DOWN como un inventario conjunto y
evaluar la secuencia completa de ejecuciones. No proporciona una estrategia
reproducible. Faltan las reglas exactas de entrada, el tipo de orden, la
atribución de fills, las comisiones, el slippage, los rechazos y la selección de
mercados. Sus cuentas y cifras no fueron verificadas y no se usaron como
evidencia de rentabilidad.

La hipótesis comprobable elegida fue la rotación temporal mínima: comprar cinco
shares del lado barato y, después, comprar cinco del lado opuesto solo si el
coste ejecutable total queda en 0,97 o menos. Si el hedge no aparece, la primera
pierna se mantiene hasta resolución.

## Controles

- Fuente histórica sellada: `data/silver_completo_v3_865.db`.
- Ventana total exacta: 24 horas, del 25 al 26 de julio de 2026 UTC.
- Desarrollo: primeras 16 horas; holdout: últimas 8 horas.
- Ask más 0,005 de slippage y comisión `0.07*p*(1-p)`.
- Profundidad mínima: cinco shares dentro de un centavo del mejor ask.
- Una posición como máximo por mercado.
- Wallet, órdenes y dinero real bloqueados.
- El forward activo no fue leído ni modificado.

## V0.16 y corrección V0.16.1

El primer protocolo exigía `quality_flags == 0` y produjo cero entradas. El
diagnóstico mostró que todas las filas tenían únicamente el bit 8, que el
propio proyecto define como Binance ausente. Esta estrategia no consume
Binance ni Chainlink. V0.16 se conservó intacto y se congeló V0.16.1 antes de
calcular rentabilidad, cambiando solo el filtro a `(quality_flags & 22) == 0`:
deben estar presentes y ser válidos los BBA de UP y DOWN.

Resultados sobre 192 mercados de desarrollo:

| Configuración | Entradas | Pares | Tasa pareada | PnL pares | PnL sin hedge | PnL neto | ROI |
|---|---:|---:|---:|---:|---:|---:|---:|
| CAUTIOUS_030 | 165 | 103 | 62,42% | +39,922 | -70,737 | -30,814 | -5,49% |
| BALANCED_035 | 175 | 110 | 62,86% | +46,608 | -91,296 | -44,687 | -7,39% |
| BROAD_040 | 188 | 119 | 63,30% | +49,682 | -110,639 | -60,958 | -9,15% |

La frecuencia fue alta y los pares cerrados ganaron. El fallo fue la cola no
cubierta: solo dos o tres de las piernas sin hedge ganaron, según la
configuración. El coste de esas pérdidas superó ampliamente el beneficio de
los pares. Veredicto sellado: `FAIL_DEVELOPMENT`. El holdout de ocho horas no
se abrió.

## V0.17: probabilidad de completar el hedge

V0.17 probó si las variables disponibles en el instante de entrada podían
identificar qué posiciones llegarían a formar el par. Dos regresiones
logísticas se entrenaron con las primeras ocho horas y se evaluaron fuera de
muestra en las ocho horas siguientes. La validación final de ocho horas siguió
sellada.

El modelo más estable alcanzó AUC 0,639. Con umbral 0,80 seleccionó 15 trades,
12 pareados, PnL +1,415 y ROI +2,42%. Sin embargo, el PnL fue +3,050 en la
primera mitad y -1,635 en la segunda. Incumplió el gate temporal congelado y no
se seleccionó modelo. Veredicto: `FAIL_DEVELOPMENT`.

## Conclusión

El post sí resolvió qué estructura investigar, pero no aportó el mecanismo que
controla el riesgo de una pierna incompleta. En nuestros datos, comprar el lado
barato esperando una reversión es frecuente, pero pierde cuando la trayectoria
no revierte. Un filtro básico mejora la tasa de pares, aunque no de forma
temporalmente estable.

No se debe lanzar un trader paper con V0.16/V0.17 ni rescatar sus umbrales. El
siguiente paso válido es capturar durante un máximo de 24 horas trayectorias
CLOB por segundo bajo el régimen actual, incluyendo fills y profundidad, sin
alterar el forward oficial. Esa base permitirá probar gestión de múltiples
fills, cancelación y salida de la pierna incompleta con datos contemporáneos.

## Reproducción

```cmd
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe investigar_v016_inventory_rotation.py
.\.venv\Scripts\python.exe preparar_v017_hedge_completion.py
```

Las salidas están selladas y los comandos rechazan sobrescribirlas.
