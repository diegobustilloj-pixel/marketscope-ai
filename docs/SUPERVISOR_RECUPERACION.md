# Supervisor de recuperacion de PolyMarker QuantBot

El supervisor mantiene seis procesos paper-only base y dos procesos
condicionales prerregistrados:

1. forward TWAP v0.9.4a1;
2. execution collector v0.12;
3. monitor v0.13;
4. paper trader v0.13;
5. monitor v0.14;
6. paper trader v0.14.
7. monitor v0.15, solo si v0.14 selecciona un candidato valido;
8. paper trader v0.15, bajo la misma condicion.

No contiene wallet, API privada ni envio de ordenes. El supervisor y el cierre
automatico no consultan labels crudos. V0.15 solo puede leer exactamente sus
10 labels al completar sus 10 trades. La auditoria forward solo se ejecuta
despues de `experiment_completed_at`. Cada comando es una lista fija y se
ejecuta sin shell.

## Protecciones

- bloqueo de instancia unica para impedir dos supervisores;
- adopcion inicial solo si PID, ejecutable y marcadores del comando coinciden;
- token de creacion de Windows para detectar reutilizacion de PID;
- hashes congelados verificados antes de cada arranque;
- cierre automatico protegido por hashes: activa v0.15 desde el resultado
  sellado v0.14 y audita el forward una sola vez al terminar;
- los workers v0.15 permanecen `WAITING_DEPENDENCY` hasta conocer v0.14 y
  quedan `NOT_APPLICABLE` si no hay candidato;
- si existe un Python desconocido del mismo entorno, no inicia duplicados;
- comprobacion cada 10 segundos y reinicio progresivo de 10 a 300 segundos si
  un proceso termina;
- un proceso vivo con heartbeat atrasado se marca degradado, pero no se mata;
- al existir el archivo de resultado correspondiente, no vuelve a iniciar ese
  workflow;
- variables de API keys, passphrases, claves privadas, secretos y wallet se
  eliminan del entorno de los procesos iniciados.

El execution collector y los paper traders supervisados se ejecutan en bloques
de 24 horas. Sus bases son reanudables e idempotentes; el supervisor inicia el
siguiente bloque solo mientras el resultado correspondiente siga pendiente.

## Recuperacion despues de un corte

La tarea `PolyMarker QuantBot Supervisor` se inicia al volver a entrar en
Windows. Lee el estado persistido, valida que los PID anteriores ya no existen
y recupera los procesos pendientes. No necesita ventanas CMD abiertas.

El inicio es posterior al login de Windows; ningun programa puede recolectar
datos mientras el equipo esta apagado.

## Estado y registros

- estado legible: `data/supervisor_quantbot_status.json`;
- identidad de procesos: `data/supervisor_quantbot_state.json`;
- registro del supervisor: `data/supervisor_quantbot.log`;
- salidas futuras: `data/supervisor_logs/`.

`ver_estado_supervisor_quantbot.bat` muestra el estado. El archivo
`detener_supervisor_quantbot.bat` detiene solo el supervisor y deja vivos los
collectors y monitores.

`ver_estado_general.bat` muestra en una sola pantalla el avance, las horas
estimadas de v0.13/v0.14/forward, el estado de v0.15 y el cierre automatico.

La tarea programada se elimina con:

```powershell
powershell -ExecutionPolicy Bypass -File .\instalar_supervisor_windows.ps1 -Remove
```
