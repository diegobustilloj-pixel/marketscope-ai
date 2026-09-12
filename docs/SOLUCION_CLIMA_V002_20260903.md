# Solución operativa — estrategia manual de clima V002

Fecha de corte: 2026-09-03 (America/La_Paz)

## Resultado

Los problemas corregibles del programa y del flujo de datos quedaron resueltos. La
estrategia permanece deliberadamente en modo `PAPER_ONLY` y hoy emite `NO_ENTRY`:
no existe una entrada que cumpla simultáneamente calidad del modelo, ventana
temporal, liquidez y ventaja neta después de comisiones.

La ausencia de un historial sincronizado de libros de órdenes no puede repararse
retroactivamente. Se resolvió de forma segura: V002 guarda desde ahora cada precio,
profundidad, pronóstico, regla y comisión observados, y no habilitará dinero real
hasta completar la prueba prospectiva prerregistrada.

## Problemas reparados

- El universo ya no queda anclado a mercados vencidos: descubre y pagina todos los
  eventos activos y actualiza el universo cada hora.
- La interpretación del contrato valida estación, variable, unidad, precisión,
  periodo diario y fuente de resolución. La zona horaria IANA se deriva de las
  coordenadas exactas de la estación y se guarda junto al pronóstico.
- Las decisiones usan ventaja **neta**. La comisión se captura al observar la entrada
  y se bloquea el contrato si falta o cambia su calendario de comisiones.
- El precio de entrada simulado incorpora un impacto conservador de un centavo,
  además de límites de spread, profundidad y tamaño.
- Se impide mezclar silenciosamente contratos cuyas reglas, estación, unidad,
  precisión, cubetas o comisiones hayan cambiado.
- El backtest y la señal en vivo usan la misma definición económica.
- La salida separa la mayor probabilidad bruta de la mejor probabilidad D+1 con
  calidad suficiente, evitando presentar como recomendación una cifra no operable.
- No existe integración de cartera, firma ni envío de órdenes: la entrada continúa
  siendo exclusivamente manual.

## Estado comprobado

- Contratos activos actuales: 249.
- Contratos que pasan la validación operativa estricta: 249 de 249.
- Ciclos V002 completos: 5 de 5.
- Errores de recolección: 0.
- Pronósticos almacenados: 5,728.
- Cotizaciones almacenadas: 15,752.
- Pruebas automatizadas: 579 aprobadas, 0 fallidas.

## Señal actual

`NO_ENTRY`.

La mejor probabilidad D+1 con calidad suficiente fue Karachi, máxima del 4 de
septiembre, cubeta de 32 °C:

- Probabilidad del modelo: 42.56%.
- Mejor venta observada: 0.43.
- Entrada conservadora: 0.44.
- Comisión estimada por participación: 0.01232.
- Ventaja neta: -2.67 puntos porcentuales.
- Además, la ventana de entrada había vencido y faltaba profundidad.

Por tanto, 42.56% es la probabilidad de mayor calidad observada, pero **no es una
recomendación de compra**. La probabilidad bruta más alta fue 96.5% en Madrid para
una mínima del 5 de septiembre; no es operable porque es D+2, tiene muestra
insuficiente y precio/ventaja desfavorables.

## Resultado del backtest V002

Todavía hay 0 operaciones V002 oficialmente resueltas. Los 156 estados observados
continúan abiertos o ambiguos, de modo que no existe base válida para declarar la
estrategia rentable. El veredicto correcto es `MORE DATA REQUIRED`.

La V001 tampoco demostró rentabilidad: no tuvo operaciones que pasaran todos los
filtros oficiales y su única operación provisional perdió la apuesta completa.

## Regla para habilitar dinero real

V002 solo puede salir de `PAPER_ONLY` después de al menos 30 entradas prospectivas,
independientes y oficialmente resueltas, y únicamente si se cumplen a la vez:

- PnL acumulado positivo.
- ROI acumulado positivo.
- Profit factor mínimo de 1.25.
- Límite inferior del intervalo bootstrap del 95% para PnL medio mayor que cero.

Hasta entonces, cada señal es observación en papel y ninguna etiqueta de
"rentable" es válida.

## Uso

1. Ejecutar `actualizar_senal_clima.bat` para refrescar y abrir la señal actual.
2. Ejecutar `iniciar_monitoreo_clima_v002_24h.bat` para capturar automáticamente
   datos durante 24 horas; el monitor no realiza apuestas.
3. Ejecutar `detener_monitoreo_clima_v002.bat` para detenerlo.
4. Ejecutar `actualizar_backtest_clima_v002.bat` después de que los mercados se
   resuelvan para recalcular la evidencia.

El diseño prioriza no apostar cuando la evidencia es insuficiente. Ninguna
estrategia garantiza beneficios y el rendimiento en papel no asegura rendimiento
futuro.
