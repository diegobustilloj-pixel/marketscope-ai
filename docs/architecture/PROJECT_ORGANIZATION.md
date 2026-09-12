# Organización profesional de ProyectoBotV4

## Decisión

`C:\ProyectoBotV4\polymarket_quant_bot` es la fuente canónica. V2 y V3 son históricos. No se crearán copias completas V5/V6 para probar estrategias: cada nueva idea será un bot configurado dentro de esta plataforma.

## Capas

```text
apps/                 envoltorios de bots nuevos
configs/bots/         un manifiesto por bot
configs/environments/ límites por ambiente
src/polymarket_bot/   código compartido y módulos existentes
schemas/              contratos de configuración
tests/                pruebas automatizadas
data/                 evidencia local grande, fuera de Git
logs/                 ejecución local, fuera de Git
research/             índice de investigaciones; los datasets siguen en data/
artifacts/             inventarios pequeños y entregables versionables
archive/               paquetes y copias históricas, no ejecutables
docs/                  decisiones, protocolos y operación
tools/                 utilidades de mantenimiento del repositorio
```

## Compatibilidad

Los scripts `.py` y `.bat` existentes en la raíz permanecen en su ubicación porque algunos son lanzadores usados por tareas o ventanas de Windows. No se moverán hasta identificar consumidores y reemplazarlos por comandos estables. Los módulos `v013`–`v060` también conservan sus imports.

La raíz se considera una **capa de compatibilidad**, no el destino de código nuevo.

## Regla para código nuevo

1. Crear el bot con `manage_bots.py create`.
2. Colocar lógica compartida bajo `src/polymarket_bot/` y no copiarla entre apps.
3. Mantener en `apps/<bot-id>/main.py` solo el ensamblaje del bot.
4. Añadir pruebas en `tests/`.
5. Escribir datos únicamente en `data/<bot-id>/`.
6. Registrar cada experimento con `run_id`, configuración, versión y resultado.

## Estados permitidos

```text
draft → research → backtest → shadow → paper → approved → live
                                    ↘ paused → archived
```

En la implementación actual `live` está bloqueado por código. `approved` expresa revisión de evidencia, no autorización automática de capital.

## Responsabilidad por carpeta

| Carpeta | Versionar en Git | Contenido |
|---|---:|---|
| `src`, `tests`, `configs`, `schemas`, `docs` | Sí | Código y contratos reproducibles |
| `artifacts/inventory` | Sí | Inventarios pequeños sin secretos |
| `data`, `logs` | No | Bases, capturas y resultados locales |
| `archive/packages` | No | ZIP recuperables |
| `.env`, claves, semillas | Nunca | Secretos locales |

## Próxima refactorización

Después de estabilizar el registro, migrar por dominio en PRs pequeños:

- `src/polymarket_bot/ledger/`
- `src/polymarket_bot/wallets/`
- `src/polymarket_bot/strategies/climate/`
- `src/polymarket_bot/strategies/sports/`
- `src/polymarket_bot/strategies/btc5m/`
- `src/polymarket_bot/execution/`

Cada movimiento debe incluir imports compatibles o wrappers y pasar la suite antes de retirar la ruta antigua.
