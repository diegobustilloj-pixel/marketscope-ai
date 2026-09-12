# Política de datos y artefactos

## Principio

Código y configuración pertenecen a Git. Evidencia pesada pertenece a `data/`. Logs pertenecen a `logs/`. Secretos no pertenecen a ninguna carpeta versionada.

## Niveles

| Nivel | Ejemplo | Mutabilidad | Git |
|---|---|---:|---:|
| Raw | mensajes WS, respuestas API, logs Polygon | Inmutable | No |
| Normalized | eventos decodificados, Silver | Reconstruible | No |
| Features | Gold/model inputs | Reconstruible y versionado por manifest | No |
| Results | backtests, shadow, auditorías | Sellar por run ID | No, salvo resumen pequeño |
| Knowledge | decisiones, esquemas, contratos | Revisable | Sí |

## Reglas

- Nunca modificar raw para “corregirlo”; producir una nueva transformación.
- Usar enteros atómicos o `Decimal` para dinero, nunca `float` como verdad contable.
- Toda salida debe apuntar a fuente, versión de código y configuración.
- No compartir el mismo archivo DB para dos escritores.
- Usar sufijo `.partial` mientras una salida no esté sellada.
- No subir bases, ZIP, modelos, logs ni CSV masivos a Git.
- Hacer backup externo de datos irremplazables; `.gitignore` no es backup.

## Datos actuales

Los aproximadamente 11,4 GB existentes permanecen en sus rutas para no romper análisis ni procesos. `tools/build_project_catalog.py` genera un inventario de primer nivel sin leer el contenido sensible de las bases.

## Secretos

`.env.example` documenta nombres, nunca valores reales. Private keys, seed phrases y credenciales no deben aparecer en `.env.example`, logs, informes, configuraciones de bots ni commits.
