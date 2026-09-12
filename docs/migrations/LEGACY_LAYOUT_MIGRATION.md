# Migración de la disposición histórica

## Estado inicial observado

- Más de 200 módulos en `src/polymarket_bot`.
- Decenas de wrappers Python/BAT en la raíz.
- Versiones experimentales `v013`–`v060` con imports y pruebas existentes.
- Aproximadamente 11,4 GB bajo `data/`.
- ZIP, `.bak` y archivos huérfanos mezclados con ejecutables.
- Sin repositorio Git al comenzar esta reorganización.

## Lo migrado ahora

- Catálogo declarativo de bots y entornos.
- Generador fail-closed de bots nuevos.
- Política Git/data/secrets.
- ZIP y copias no ejecutables trasladados a `archive/` o `archives/`.
- CI, schema, documentación y catálogo automático.

## Lo que no se mueve todavía

- Bases y capturas.
- Módulos con imports activos.
- Wrappers `.py` y `.bat`.
- Logs de ejecuciones existentes.

Moverlos todos de una vez convertiría una limpieza visual en un riesgo operacional. La migración futura será por dominio y con compatibilidad.

## Criterio para retirar un wrapper histórico

Solo retirar cuando:

1. Existe un entrypoint estable equivalente.
2. No hay BAT, tarea o documentación que dependa de la ruta.
3. Las pruebas cubren ambos comportamientos.
4. El nuevo comando funcionó al menos en smoke/shadow.
5. Se registra el reemplazo en el changelog.
