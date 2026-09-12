# Actualización segura a la Fase 3

## Bases que deben conservarse en V4

- `data\silver_completo_v3_865.db`;
- `data\silver_completo_v4_25.db`.

La Fase 3 abre ambas con SQLite `mode=ro`. No las copie ni mueva.

## Actualización

1. Cierre todas las ventanas CMD del bot.
2. Abra el ZIP 0.6.1.
3. Extraiga su carpeta `polymarket_quant_bot` encima de
   `C:\ProyectoBotV4\polymarket_quant_bot`.
4. Elija **Reemplazar los archivos en el destino**.
5. Ejecute `instalar_windows.bat`.
6. Ejecute `corregir_dataset_gold_fase3.bat`.

La extracción encima de V4 conserva la carpeta `data`; el programa nunca
sobrescribe una base Gold existente.

## Si aparece un error

No elimine archivos ni repita automáticamente. Envíe:

- `auditoria_fase3_gold_v2.txt`, si llegó a generarse;
- una captura completa de la ventana.

La Fase 3 continúa sin wallet, claves, firma de órdenes, IA ni dinero real.
