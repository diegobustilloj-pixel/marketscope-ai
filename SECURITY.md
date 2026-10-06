# Seguridad y publicación responsable

Este repositorio está diseñado para investigación, shadow y auditoría. No contiene ni debe contener una wallet conectada, firma, retiros, órdenes automáticas o dinero real.

## Nunca versionar

- claves privadas, frases semilla, contraseñas, tokens o API keys;
- archivos `.env` reales, credenciales de RPC privadas o cabeceras de autorización;
- bases de datos, capturas raw, logs, resultados masivos, archivos `.partial` o backups ZIP;
- información personal o datos de terceros que no estén autorizados para publicación.

`.env.example` puede documentar nombres de variables sin valores sensibles. Si detectas una credencial en un archivo rastreado, no la copies a un issue, chat o commit: revócala o rótala primero y elimina su exposición del historial antes de publicar.

## Límites de operación

Una métrica, backtest, captura, monitor shadow o reconciliación no autoriza trading. Toda acción que conecte una wallet, firme, envíe una orden, retire fondos o transmita datos fuera del proyecto requiere autorización explícita y separada.

## Reporte de un problema

Para un hallazgo sensible, evita abrir un issue público con los detalles. Comunícalo al propietario del proyecto por un canal privado acordado, incluyendo la ruta afectada y una descripción sin secretos.
