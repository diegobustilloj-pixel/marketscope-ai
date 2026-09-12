# Fase 5 — motor de riesgo paper-only

Esta fase prepara controles de cartera sin habilitar wallet, API privada ni
ordenes reales. No esta conectada a v0.13 ni v0.14 mientras esas muestras
siguen abiertas.

## Estado actual

`data/paper_risk_profile_draft.json` es deliberadamente un borrador bloqueado.
Solo contiene limites demostrables a partir de los prerregistros actuales:

- maximo 5 shares por propuesta;
- maximo 1.50 de desembolso simulado por propuesta, derivado de
  `5 * 0.30`.

No se inventaron limites de exposicion simultanea, perdida de sesion,
drawdown ni racha de perdidas. Permanecen nulos hasta que una estrategia
supere desarrollo y confirmacion independiente.

## Comportamiento fail-closed

El modulo `polymarket_bot.paper_risk` no puede activarse con:

- un perfil que no sea `FROZEN_PAPER_ONLY`;
- un limite obligatorio ausente;
- Kelly, apalancamiento o capital compuesto;
- wallet, API privada u ordenes habilitadas;
- hashes de fuentes distintos.

Incluso con un perfil futuro valido, el motor solo registra fills y
liquidaciones simuladas en memoria. No contiene cliente HTTP, firma, wallet ni
funcion para enviar ordenes.

## Controles previstos

- maximo de shares y desembolso por propuesta;
- maximo de posiciones y efectivo paper abierto;
- limite de perdida de sesion;
- limite de drawdown;
- limite de perdidas consecutivas;
- kill switch manual;
- rechazo de datos stale, mercado no ejecutable, duplicados y cualquier
  propuesta marcada como dinero real.

El perfil solo podra congelarse despues de seleccionar una estrategia futura.

Los limites de perdida y drawdown se comprueban contra el peor caso: PnL
realizado menos el desembolso de todas las posiciones abiertas y de la nueva
propuesta. De este modo varias posiciones simultaneas no pueden sobrepasar el
tope aunque todavia no se hayan liquidado.
