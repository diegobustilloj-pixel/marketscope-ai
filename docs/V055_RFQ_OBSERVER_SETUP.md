# V0.55 RFQ Observer — configuración segura

V0.55 observa durante una hora el canal oficial RFQ para market makers. Solo envía el
mensaje inicial `auth`. No puede enviar cotizaciones, cancelaciones, confirmaciones,
órdenes, firmas ni transacciones.

## Variables requeridas

Configúralas únicamente en la ventana de CMD que ejecutará el observador. No las escribas
en el código, el chat, capturas de pantalla ni archivos del proyecto.

```bat
set "PM_RFQ_API_KEY=<CLOB_API_KEY>"
set "PM_RFQ_API_SECRET=<CLOB_API_SECRET>"
set "PM_RFQ_API_PASSPHRASE=<CLOB_API_PASSPHRASE>"
set "PM_RFQ_SIGNER_ADDRESS=<0x_ADDRESS>"
set "PM_RFQ_MAKER_ADDRESS=<0x_ADDRESS>"
set "PM_RFQ_SIGNATURE_TYPE=<0|1|2|3>"
```

Tipos de firma documentados por Polymarket:

- `0`: EOA; signer y maker deben ser la misma dirección.
- `1`: Proxy wallet.
- `2`: Safe wallet.
- `3`: Deposit wallet; signer y maker deben ser la misma dirección.

V0.55 no necesita ni acepta una clave privada. Las credenciales se leen solamente del
entorno del proceso y no se guardan en la base ni en los resultados.

## Verificación y ejecución

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe v055_monitor.py --preflight
.\.venv\Scripts\python.exe v055_monitor.py --run
```

En otra ventana puedes revisar el avance:

```bat
cd /d C:\ProyectoBotV4\polymarket_quant_bot
.\.venv\Scripts\python.exe v055_monitor.py --status
```

Al terminar la hora:

```bat
.\.venv\Scripts\python.exe v055_monitor.py --audit
```

No pegues valores secretos en este documento. Si una variable falta, el preflight se
detiene antes de crear la base o abrir la conexión de red.
