# Changelog

## Unreleased

### Estructura profesional

- Proyecto V4 declarado como fuente canónica.
- Registro declarativo y validador de bots.
- Ambientes separados para desarrollo, backtest, shadow, paper y producción.
- Política de datos, runbook y plan de migración compatibles con las rutas existentes.
- Datos, logs, secretos, bases, modelos y paquetes históricos excluidos de Git.
- Preparación de CI para ejecutar la suite de pruebas.
- Archivo no destructivo de 20 paquetes, copias y documentos, con manifiesto SHA-256.
- Inventario reproducible de bots, archivos raíz y 11,31 GiB de datos locales.
- Repositorio Git inicializado en la rama `main`; primera instantánea preparada para commit.

No se habilitaron órdenes automáticas ni dinero real.

### PolyLedger P0

- Calificación reproducible de fuente Sourcify contra bytecode RPC fijado a un
  bloque, con soporte explícito para despliegues directos y proxies EIP-1967.
- PositionManager Combo V2 identificado como proxy y vinculado a su
  implementación verificada; ABI, fuente y layout de IDs quedan sellados por hash.
- Transferencias Combo ERC-1155 decodificadas con sus campos estructurales de
  posición. La economía de módulos, conversiones y resoluciones sigue bloqueada.
- Captura RPC acepta un endpoint HTTPS explícito sin habilitar credenciales,
  firma, wallet, órdenes ni dinero real.
- Gate `pilot-readiness` para censar archivos históricos en solo lectura y
  rechazar muestras sin identidad, completitud, basis, ciclo on-chain, balances
  al mismo corte o PnL independiente.
- Captura sellada de 24 horas por wallet: actividad paginada, logs indexados,
  cierre sobre recibos completos, saldos en ambos cortes y observación de
  contratos. Admite proveedores separados y reconcilia transferencias contra
  balances sin interpretar el flujo como PnL.

### Backlog de investigación

- Registradas las conclusiones de Balthazar, `car`, `e46m3`, `Oxp3mny` y la forensia GitHub.
- Definido el orden de construcción: PolyLedger P0 → OPS Sentinel → NegRisk → Balthazar → `car`.
- Las cinco construcciones permanecen pendientes y deshabilitadas.
