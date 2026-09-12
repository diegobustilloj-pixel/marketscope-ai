# Auditoría de RetroValix y oportunidad de producto para QuantBot

Fecha de corte: 14 de agosto de 2026  
Perfil: [VALIX — @RetroValix](https://x.com/RetroValix)  
Objetivo: identificar una oportunidad construible, comercializable y respaldada por el comportamiento del perfil y su audiencia.

## Veredicto ejecutivo

La mejor oportunidad no es lanzar otro “bot que gana” ni reconstruir un simulador genérico. Es convertir la infraestructura ya creada en QuantBot en un **laboratorio de verificación reproducible de estrategias de Polymarket**, provisionalmente llamado **Bot Truth Lab**.

El usuario pega una wallet, un post o una hipótesis. El sistema reconstruye el comportamiento observable, obliga a expresar reglas comprobables, ejecuta una simulación realista y una prueba forward paper, y entrega un informe que separa:

- lo observado;
- lo inferido;
- lo que no puede saberse;
- lo que funcionó en muestra;
- lo que sobrevivió fuera de muestra;
- y lo que falló por frecuencia, fills, latencia, slippage, riesgo o inestabilidad.

La evidencia converge desde tres lados:

1. RetroValix publicó aproximadamente **125 posts centrados en bots o microestructura** dentro de las 488 publicaciones originales accesibles. Su proceso recurrente consiste en analizar ejecuciones y explicar una estrategia con Claude.
2. La audiencia pide repetidamente cómo obtener, copiar, aprender o reproducir esos bots, pero también cuestiona si los resultados son reales y si el edge sobreviviría a latencia, comisiones, capital, volatilidad y competencia.
3. QuantBot ya demostró el valor comercial de la falsación: V0.13/V0.14 fallaron por frecuencia y V0.16/V0.17 mostraron que una narrativa atractiva de inventario UP/DOWN pierde cuando se modelan las piernas sin hedge y la estabilidad temporal.

La conclusión contraria también es importante: **no competir de frente como simulador, copy-trader, tracker de wallets ni vendedor de datos históricos**. Esos espacios ya tienen competidores activos y mejor posicionados. El ángulo defendible es “de afirmación pública a evidencia reproducible”, empezando como herramienta interna/servicio y solo después como SaaS.

## 1. Alcance, método y límites

### Cobertura conseguida

- 488 publicaciones originales accesibles en la pestaña Posts.
- Fecha más reciente incluida en esa extracción: 13 de agosto de 2026.
- Fecha más antigua expuesta por X: 14 de junio de 2022.
- 245 comentarios o respuestas, excluyendo las tres publicaciones principales duplicadas al abrir conversaciones.
- Conversaciones completas accesibles de tres publicaciones de alta señal:
  - [bot de $178.000](https://x.com/RetroValix/status/2032506967923515706): 92 comentarios/respuestas accesibles;
  - [bot de $81.000](https://x.com/RetroValix/status/2057127796443377791): 35 comentarios/respuestas accesibles;
  - [guía de insiders](https://x.com/RetroValix/status/1999468100882723025): 83 comentarios/respuestas accesibles.
- 33 respuestas recientes del propietario recogidas en la pestaña Replies.
- Lanzamiento y conversación de [PaperMarket](https://x.com/RetroValix/status/2003545992101089433), más revisión de su perfil y dominio.

La extracción cruda está en `data/retrovalix_posts_raw.json` y los comentarios están separados por ID de conversación en `data/retrovalix_comments_*.json`. El anexo [RETROVALIX_POST_POR_POST_20260814.md](RETROVALIX_POST_POR_POST_20260814.md) contiene las 488 fichas individuales, desde la publicación más reciente hasta la más antigua.

### Límites que no deben ocultarse

- El perfil muestra 5.144 posts totales porque X incluye respuestas. X no permitió recorrer de forma estable las más de cinco mil entradas de Replies; se analizó una muestra reciente y las conversaciones de mayor señal, no todas las respuestas históricas.
- La pestaña Articles quedó bloqueada en “Loading…” tras la paginación intensiva. Los artículos que aparecieron en Posts sí quedaron registrados, pero no puede afirmarse que la lista de artículos sea exhaustiva.
- Los reposts solo se incluyeron cuando aparecieron como quote/contexto relevante. No se auditó de forma exhaustiva la pestaña Reposts.
- X recortó 311 textos en la cronología. Se recuperó el texto completo de 89 posts antes de que X limitara nuevas cargas; en los restantes, el anexo usa únicamente el fragmento visible y declara la limitación.
- No se reprodujeron cuadro por cuadro todos los videos. Se usaron el texto, las etiquetas accesibles, capturas descritas y el contexto del post.
- No se atribuye causalidad entre usar Claude y obtener PnL. “Built with Claude” es una afirmación del post, no una verificación del proceso de desarrollo.
- “Insider” se usa como término del autor. Detectar una wallet inusual no prueba información privilegiada ni intención ilegal.

### Cómo se separó evidencia de interpretación

- **Hecho observado:** texto, fecha, métricas, enlace, comportamiento del sitio o resultado reproducible de QuantBot.
- **Interpretación:** explicación razonada que conecta varios hechos.
- **Hipótesis:** oportunidad que todavía debe validarse con compradores.

## 2. Evolución del perfil

### 2022–2024: curador de oportunidades cripto/NFT

La primera etapa disponible está dominada por proyectos tempranos, free mints, allowlists, nodos, testnets y señales sociales. El patrón no es una sola tecnología; es el trabajo repetitivo de descubrir, filtrar, explicar y publicar oportunidades antes que otros. La debilidad es que muchas señales usadas —seguidores, influencers o actividad social— pueden manipularse y no existe un registro visible de precisión posterior.

### Agosto–octubre de 2025: reactivación y transición a Polymarket

El perfil vuelve a publicar con fuerza y se concentra en prediction markets. Aparecen educación sobre UMA, liquidez, rewards, reglas, estadísticas y el programa para builders. Esto muestra dos intenciones: dominar el ecosistema y construir dentro de él.

### Noviembre–diciembre de 2025: builder, investigación de insiders y PaperMarket

En [19 de noviembre](https://x.com/RetroValix/status/1991123900076269879) dice que construye una aplicación para Polymarket. En [23 de diciembre](https://x.com/RetroValix/status/2003545992101089433) anuncia que es cofundador de PaperMarket, construido durante unos dos meses.

PaperMarket ofrecía:

- USDC virtual;
- precios, gráficos y libros copiados de Polymarket mediante API;
- ejecución simulada al precio/libro vigente;
- ausencia de conexión de wallet;
- práctica antes de operar con dinero real.

La cuenta de PaperMarket solo muestra tres publicaciones y 165 seguidores. El 14 de agosto de 2026, `papermarket.app` respondió **“Service Suspended”**. No hay evidencia accesible suficiente para afirmar por qué se suspendió; sí demuestra que construir la primera versión no resolvió distribución, retención u operación sostenible.

En paralelo, la [guía de insiders](https://x.com/RetroValix/status/1999468100882723025) revela un proceso manual de cuatro pasos. En los comentarios, RetroValix afirma que unir esos cuatro pasos ahorraría tiempo. Este es uno de los problemas más explícitos del perfil.

### Enero–abril de 2026: herramientas, traders y automatización

El contenido combina terminales, noticias en tiempo real, clusters de wallets, copy trading, rewards y análisis de traders automatizados. El perfil actúa como investigador y distribuidor de herramientas. La audiencia aprende, pero el resultado suele quedarse en una explicación, un enlace o una promoción.

### Mayo–agosto de 2026: fábrica diaria de análisis de bots

La actividad converge en bots de crypto Up/Down, microestructura, market making, arbitraje, directional skew, inventario y hedging. Claude aparece repetidamente como herramienta de análisis o construcción. El [artículo de cinco bots rentables](https://x.com/RetroValix/status/2087291446357348440) dice haber analizado más de un millón de ejecuciones.

Este patrón prueba demanda de contenido, pero también crea su propia objeción: si se publican resultados extraordinarios casi a diario sin protocolo reproducible, parte de la audiencia empieza a percibirlos como promoción o “too good to be true”.

## 3. Qué dice que quiere y qué realmente parece necesitar

### Lo que dice que quiere

- encontrar y explicar traders rentables;
- construir con IA;
- detectar insiders/wallets inusuales;
- crear herramientas para Polymarket;
- enseñar estrategias;
- practicar antes de usar dinero real;
- automatizar trading.

### Lo que su comportamiento indica que necesita

1. **Convertir análisis narrativos en especificaciones reproducibles.** Hace el mismo trabajo de ingeniería inversa una y otra vez.
2. **Reducir el costo de verificación.** Analizar cientos de miles de ejecuciones manualmente no escala.
3. **Distinguir PnL observado de edge replicable.** Una wallet pudo ganar por capital, prioridad, rebate, latencia o riesgo no visible.
4. **Cerrar el bucle posterior a la publicación.** No basta describir una estrategia; hay que observar si sigue funcionando.
5. **Ganar credibilidad con informes falsables.** La audiencia pide “¿es real?”, “¿cuál fue el drawdown?” y “¿sobrevive a fees/slippage?”.
6. **Un producto con distribución incorporada.** PaperMarket prueba que construir sin canal de adopción no es suficiente.

## 4. Comentarios relevantes analizados uno por uno

La siguiente tabla conserva los comentarios que cambian la decisión. “Repetido” significa que la misma necesidad aparece en otros usuarios de las conversaciones revisadas.

| Usuario y evidencia | Qué dice | Problema / deseo / objeción | Repetido | Oportunidad |
|---|---|---|---|---:|
| [@Theamerica34496](https://x.com/Theamerica34496/status/2032815277944004835) | Pregunta si existe una formación para aprender a hacerlo. | Falta una ruta práctica desde explicación hasta bot probado. | Sí | 9/10 |
| [@LatInfoSec](https://x.com/LatInfoSec/status/2032870387177107507) | Pregunta cómo conseguir el bot. | La audiencia quiere una salida ejecutable, no solo el relato. | Sí | 9/10 |
| [@panfila](https://x.com/panfila/status/2032884111300333818) | Pregunta nombre y dónde comprarlo. | Señal de intención de pago, aunque basada en una promesa no verificada. | Sí | 8/10 |
| [@geunho7349](https://x.com/geunho7349/status/2073518885564010785) | Pregunta cómo obtener el programa. | Demanda de acceso. | Sí | 8/10 |
| [@MQLhuson](https://x.com/MQLhuson/status/2032799109422174510) | Pregunta si puede hacerse copy trade. | Confunde observar transacciones con poder replicarlas. | Sí | 9/10 |
| [@RetroValix](https://x.com/RetroValix/status/2032801769130987814) | Responde que es imposible copiar bots de ese tipo. | Confirma que copy trading no resuelve HFT. | Sí | 10/10 |
| [@Sailoshi](https://x.com/Sailoshi/status/2032809872153006337) | Pregunta si alguien construyó un bot propio con resultado positivo. | Falta evidencia independiente de reproducibilidad. | Sí | 10/10 |
| [@TopGun0101](https://x.com/TopGun0101/status/2032822581120602540) | Afirma que sin servidor HFT de ~1 ms no se encontrará edge y que ya compiten empresas. | Latencia e infraestructura pueden hacer inalcanzable el resultado. | Sí | 10/10 |
| [@oxox097](https://x.com/oxox097/status/2032846104551735421) | Pregunta de dónde salen dashboards con el mismo estilo. | Falta trazabilidad y se percibe contenido templado/promocional. | Sí | 7/10 |
| [@apexvolumetrics](https://x.com/apexvolumetrics/status/2032921173319713085) | Pregunta qué pasa con volatilidad y cuando los bots compiten. | Edge decay y cambio de régimen. | Sí | 10/10 |
| [@chrismilas](https://x.com/chrismilas/status/2032797272749089138) | Pregunta si cientos de copias comprimirían el edge. | La publicación del patrón puede destruirlo. | Sí | 9/10 |
| [@GaneevSingh9](https://x.com/GaneevSingh9/status/2033816855442559018) | Cuestiona si 13,9% de edge es demasiado optimista. | Métrica extraordinaria sin intervalo ni protocolo visible. | Sí | 9/10 |
| [@RachelQuant0505](https://x.com/RachelQuant0505/status/2032851303593029758) | Señala compresión de spread y pregunta por drawdown en alta volatilidad. | Falta distribución de riesgo, no solo PnL acumulado. | Sí | 10/10 |
| [@tonitrades_](https://x.com/tonitrades_/status/2032792784847966629) | Sostiene que el edge real incluye $4,6 M de capital y capacidad de soportar drawdowns. | El código no replica capital, riesgo ni prioridad. | Sí | 10/10 |
| [@JonesLauriano](https://x.com/JonesLauriano/status/2032958396710687156) | Objeta la aplicación del modelo Stoikov y la falta de validación suficiente. | Una explicación matemática puede sonar rigurosa sin probar ejecución. | Sí | 9/10 |
| [@SL7D3](https://x.com/SL7D3/status/2032870017839108421) | Califica el post de promoción pagada engañosa. | Crisis de confianza y riesgo reputacional. | Sí | 10/10 |
| [@alohunc](https://x.com/alohunc/status/2032866546863976753) | Dice que tantos posts similares parecen sketchy/scammy. | Repetición de formato reduce credibilidad. | Sí | 9/10 |
| [@turkonthelurk](https://x.com/turkonthelurk/status/2032927880993513955) | No cree que alguien compartiría un edge real. | Incentivos del divulgador no están claros. | Sí | 9/10 |
| [@shojibranding](https://x.com/shojibranding/status/2057401144071389275) | Pide que expliquen cómo reproducirlo. | Falta puente entre análisis y experimento. | Sí | 10/10 |
| [@tonitrades_](https://x.com/tonitrades_/status/2057412065099149544) | Pregunta si fees/slippage eliminan edge en mercados delgados y alta volatilidad. | Simulación de ejecución insuficiente. | Sí | 10/10 |
| [@leanxbt](https://x.com/leanxbt/status/2057215826206331392) | Pregunta si el sniping conserva edge tras fees de Polymarket. | Rentabilidad bruta no equivale a neta. | Sí | 9/10 |
| [@Kevinle0](https://x.com/Kevinle0/status/2057342369830445400) | Pregunta cuánto puede durar el edge. | Se necesita monitor de degradación. | Sí | 9/10 |
| [@RetroValix](https://x.com/RetroValix/status/2057342173255799148) | Dice que construir con Claude es fácil, pero hacerlo funcionar exige mucho testing y fixing. | El cuello de botella es validación, no generación de código. | Sí | 10/10 |
| [@Vladic_ETH](https://x.com/Vladic_ETH/status/1999567971669229846) | Propone dashboard con alertas “wallet nueva + apuesta grande + un mercado”. | Proceso manual de insiders. | Sí | 10/10 |
| [@RetroValix](https://x.com/RetroValix/status/1999584408571924590) | Dice que usa Polysights y que unir los cuatro pasos ahorraría tiempo. | Petición explícita de integración. | Sí | 10/10 |
| [@PolySapiens](https://x.com/PolySapiens/status/2000524040327315491) | Advierte que alguien puede fingir patrón de insider para manipular. | Un detector simple es explotable. | Sí | 10/10 |
| [@RetroValix](https://x.com/RetroValix/status/2000565113418563618) | Responde que nunca depende solo de insiders y usa otras señales. | Confirma necesidad multiseñal. | Sí | 10/10 |
| [@randy_x0](https://x.com/randy_x0/status/1999607690826326113) | Dice que encontrar wallets es más fácil que probar intención. | Riesgo de acusaciones sin evidencia. | Sí | 9/10 |
| [@bz_bbvclub](https://x.com/bz_bbvclub/status/1999489263076081804) | Pide una herramienta de tracking y dice estar confundido. | Fragmentación y complejidad de herramientas. | Sí | 9/10 |
| [@polyfollowcom](https://x.com/polyfollowcom/status/2001179267095294108) | Sugiere copiar trades automáticamente. | Solución fácil pero no adecuada para latencia/HFT y con riesgo regulatorio/técnico. | Sí | 7/10 |

En el conjunto analizado hubo 188 intervenciones externas y 60 respuestas del propietario. El conteo por palabras clave no sustituye la lectura individual, pero confirma la repetición: al menos 14 comentarios estaban ligados a acceso/aprendizaje/reproducción, 10 a credibilidad y 31 a ejecución, riesgo o duración del edge.

## 5. Base maestra de problemas

### Problema 1 — La estrategia descrita no es una estrategia reproducible

- **Menciones aproximadas:** 125 publicaciones clasificadas como bots/microestructura; repetición explícita en comentarios.
- **Quién:** ambos.
- **Ejemplos:** posts diarios de bots; preguntas “¿cómo reproducirlo?” y “¿cómo conseguirlo?”.
- **Solución actual:** análisis manual con Claude.
- **Limitación:** faltan reglas, timestamps, universo, exits, fills y condiciones de rechazo.
- **Urgencia:** 10/10.
- **Disposición a pagar:** 8/10.
- **Facilidad de construir:** 8/10 aprovechando QuantBot.
- **Potencial de mercado:** 8/10.

### Problema 2 — PnL observado no prueba edge copiable

- **Menciones:** recurrente en casi cada conversación de bots.
- **Quién:** seguidores y comportamiento del perfil.
- **Solución actual:** presentar PnL, win rate, trades por hora y explicación visual.
- **Limitación:** no separa capital, rebate, selección, latencia, slippage, survivorship ni riesgo oculto.
- **Urgencia:** 10/10; **pago:** 8/10; **construcción:** 7/10; **mercado:** 9/10.

### Problema 3 — Simulación de ejecución poco realista

- **Menciones:** latencia, fees, slippage, fills y mercados delgados aparecen repetidamente.
- **Solución actual:** PaperMarket y backtests simplificados.
- **Limitación:** llenar al precio visible es optimista si no se modelan cola, tamaño, partial fills y delay.
- **Urgencia:** 10/10; **pago:** 9/10; **construcción:** 6/10; **mercado:** 8/10.

### Problema 4 — Edge decay y cambio de régimen

- **Menciones:** preguntas sobre competencia, volatilidad y duración.
- **Solución actual:** volver a observar la wallet/PnL.
- **Limitación:** PnL acumulado reacciona tarde y mezcla regímenes.
- **Urgencia:** 9/10; **pago:** 8/10; **construcción:** 7/10; **mercado:** 8/10.

### Problema 5 — Falta ruta desde post hasta experimento

- **Menciones:** formación, programa, compra, código, bot.
- **Solución actual:** artículos, hilos y vibe coding.
- **Limitación:** generar código no prueba que funcione.
- **Urgencia:** 8/10; **pago:** 8/10; **construcción:** 9/10; **mercado:** 8/10.

### Problema 6 — Investigación de wallets fragmentada

- **Menciones:** aproximadamente 68 posts de wallet/insider/copy trading y petición explícita de cuatro pasos en un lugar.
- **Solución actual:** Polysights, Arkham y búsqueda manual.
- **Limitación:** tiempo, contexto disperso y dificultad de reproducir criterios.
- **Urgencia:** 9/10; **pago:** 8/10; **construcción:** 8/10; **mercado:** 9/10.

### Problema 7 — Falsos insiders y señales manipulables

- **Menciones:** advertencias de la audiencia y respuesta multiseñal del autor.
- **Solución actual:** juicio manual y combinación con research.
- **Limitación:** reglas simples pueden ser imitadas.
- **Urgencia:** 8/10; **pago:** 7/10; **construcción:** 6/10; **mercado:** 7/10.

### Problema 8 — Copy trading no es viable para HFT

- **Menciones:** preguntas de copia y respuesta explícita del autor.
- **Solución actual:** bots de copy trading.
- **Limitación:** retraso, precio, liquidez y órdenes no equivalentes.
- **Urgencia:** 9/10; **pago:** 8/10; **construcción:** 7/10; **mercado:** 8/10.

### Problema 9 — Credibilidad de posts promocionales

- **Menciones:** 10 comentarios explícitos de real/scam/true/fake, además de colaboraciones pagadas.
- **Solución actual:** explicación más larga y visual.
- **Limitación:** más detalle narrativo no equivale a prueba.
- **Urgencia:** 10/10; **pago:** 7/10; **construcción:** 9/10; **mercado:** 8/10.

### Problema 10 — Historial de order book difícil de obtener

- **Menciones:** implícito en cualquier backtest de HFT; confirmado por el mercado de proveedores de datos.
- **Solución actual:** captura prospectiva o proveedores como DepthFeed.
- **Limitación:** costo y ausencia de datos anteriores a la captura.
- **Urgencia:** 9/10; **pago:** 9/10; **construcción:** 3/10; **mercado:** 8/10.

### Problema 11 — Riesgo y sizing ausentes

- **Menciones:** drawdown, capital, pequeñas cantidades y riesgo de pierna incompleta.
- **Solución actual:** consejos generales y Kelly en algunas narrativas.
- **Limitación:** límites sin evidencia pueden dar falsa seguridad.
- **Urgencia:** 10/10; **pago:** 8/10; **construcción:** 8/10; **mercado:** 8/10.

### Problema 12 — Distribución y sostenibilidad del producto

- **Menciones:** PaperMarket construido y hoy suspendido; cuenta con alcance bajo.
- **Solución actual:** lanzamiento desde X.
- **Limitación:** producto sin motor recurrente de adquisición/monetización.
- **Urgencia:** 9/10; **pago:** 6/10; **construcción:** 7/10; **mercado:** 8/10.

### Problema 13 — Dependencia de APIs y cambios de plataforma

- **Menciones:** uso continuo de Gamma, Data, CLOB y RTDS; cambios recientes a CLOB V2.
- **Solución actual:** adaptaciones por versión.
- **Limitación:** breaking changes, rate limits y degradación de datos.
- **Urgencia:** 8/10; **pago:** 7/10; **construcción:** 7/10; **mercado:** 7/10.

### Problema 14 — Investigación/contenido manual que no escala

- **Menciones:** análisis repetitivos diarios y un artículo con más de un millón de ejecuciones.
- **Solución actual:** Claude, scripts y redacción manual.
- **Limitación:** costo humano, consistencia y trazabilidad.
- **Urgencia:** 8/10; **pago:** 8/10; **construcción:** 9/10; **mercado:** 7/10.

### Problema 15 — Información y herramientas fragmentadas

- **Menciones:** aproximadamente 69 posts de herramientas/builders/terminales.
- **Solución actual:** múltiples productos afiliados o promocionados.
- **Limitación:** contexto cambia entre sistemas y el usuario debe recomponerlo.
- **Urgencia:** 7/10; **pago:** 7/10; **construcción:** 7/10; **mercado:** 8/10.

### Problema 16 — Calibración y seguimiento de predicciones

- **Menciones:** aproximadamente 82 posts de tesis/predicción.
- **Solución actual:** publicación puntual.
- **Limitación:** falta registro de probabilidad inicial, cambios y resultado.
- **Urgencia:** 6/10; **pago:** 6/10; **construcción:** 9/10; **mercado:** 7/10.

### Problema 17 — Riesgo legal/reputacional al llamar “insider” a una wallet

- **Menciones:** dificultad de probar intención y reglas de integridad de mercado.
- **Solución actual:** lenguaje informal.
- **Limitación:** puede producir acusaciones incorrectas o incentivar conducta prohibida.
- **Urgencia:** 9/10; **pago:** 5/10; **construcción:** 8/10; **mercado:** 6/10.

### Problema 18 — Datos pasados sin prueba contemporánea

- **Menciones:** central en todos los casos de bots.
- **Solución actual:** backtest o historial de wallet.
- **Limitación:** sesgo de selección y cambio de régimen.
- **Urgencia:** 10/10; **pago:** 9/10; **construcción:** 8/10; **mercado:** 9/10.

## 6. Patrones ocultos

### Patrón A — El producto no es el bot; es la prueba

Posts de bots, comentarios de reproducción, objeciones de latencia y la frase del propio autor (“hacerlo funcionar requiere mucho testing y fixing”) apuntan al mismo cuello de botella. El código es cada vez más barato; la evidencia de que ese código opera de forma realista es lo escaso.

### Patrón B — El contenido y el software pueden ser el mismo activo

Cada auditoría puede producir simultáneamente:

- una especificación de estrategia;
- un experimento reproducible;
- un informe público;
- una pieza de contenido para X;
- y un lead para el producto.

Esto resuelve el problema de distribución que PaperMarket no parece haber resuelto.

### Patrón C — El fracaso de QuantBot es evidencia comercial útil

V0.13/V0.14 no generaron la frecuencia esperada. V0.16 ganó en pares cerrados pero perdió por piernas sin hedge. V0.17 mejoró la selección y aun así falló estabilidad temporal. Un vendedor de señales ocultaría esto; un laboratorio de verificación lo convierte en el producto.

### Patrón D — Wallet analytics sin copiabilidad es incompleto

El perfil analiza wallets; los competidores muestran PnL y win rate; los comentarios advierten que HFT no puede copiarse. La capa faltante es responder: “¿qué porcentaje de esta ejecución habría sido alcanzable para una cuenta de X capital con Y latencia y Z slippage?”.

### Patrón E — PaperMarket llegó demasiado pronto a la interfaz

PaperMarket clonó la experiencia de trading. La audiencia actual pide algo más profundo: demostrar el edge de estrategias específicas. La nueva versión no debe empezar por mercados, charts y botones; debe empezar por un informe de evidencia y un runner paper.

### Evidencia que contradice la tesis ganadora

- DepthFeed ya ofrece order-book histórico, Backtest Lab, paper strategies, robustez y wallet intelligence.
- `papermarket.dev` ya ofrece una API paper compatible con el CLOB.
- PolySimulator ya cubre práctica manual, leaderboard, API trading y backtesting.
- La audiencia puede querer “el bot” y no pagar por un informe que diga que falla.
- La verificación HFT perfecta es imposible sin reproducir la infraestructura del trader original.

Por eso el producto debe ser más estrecho: **no prometer verdad absoluta ni competir por el mejor dataset; entregar un protocolo reproducible, límites explícitos y forward paper contemporáneo**.

## 7. Soluciones posibles

### Solución 1 — Bot Truth Lab

- **Problema:** afirmaciones de bots no reproducibles.
- **Evidencia:** 125 posts clasificados como bots/microestructura; preguntas de acceso, reproducción y credibilidad.
- **Usuario:** builders, quants, creadores y traders avanzados.
- **Funcionamiento:** wallet/post → spec → simulación → forward → informe.
- **Pago:** ahorra semanas y evita desplegar estrategias falsas.
- **Dificultad técnica:** 7/10; **costo inicial:** 4/10; **tiempo:** 5/10.
- **Primeros usuarios:** 9/10; **competencia:** 7/10; **monetización:** 9/10.
- **Necesidad:** 10/10; **ventaja:** 9/10; **factibilidad:** 9/10.

### Solución 2 — Strategy-to-Spec Copilot

- Convierte un hilo/artículo en una especificación determinista con variables, entradas, exits, universo y campos desconocidos.
- Evidencia: repetición de análisis narrativos y petición “cómo reproducirlo”.
- Dificultad 5/10; costo 3/10; tiempo 3/10; usuarios 9/10; competencia 6/10; monetización 8/10; necesidad 9/10; ventaja 7/10; factibilidad 9/10.

### Solución 3 — Wallet Copyability Audit

- Estima qué parte del PnL histórico habría sido copiable bajo capital, delay, slippage y liquidez definidos.
- Evidencia: “imposible copiar estos bots”, objeciones de latencia/capital.
- Dificultad 7/10; costo 4/10; tiempo 5/10; usuarios 8/10; competencia 7/10; monetización 8/10; necesidad 9/10; ventaja 8/10; factibilidad 8/10.

### Solución 4 — Insider Radar multiseñal

- Alertas de wallet nueva + apuesta grande + concentración, con confirmación adicional y lenguaje no acusatorio.
- Evidencia: petición explícita de dashboard, cuatro pasos y riesgo de falsos insiders.
- Dificultad 6/10; costo 4/10; tiempo 4/10; usuarios 8/10; competencia 8/10; monetización 8/10; necesidad 9/10; ventaja 6/10; factibilidad 8/10.

### Solución 5 — Edge Decay Monitor

- Reevalúa en ventanas sucesivas si una estrategia mantiene frecuencia, PnL neto, fill rate y estabilidad.
- Evidencia: preguntas “¿puede durar?” y “¿qué pasa cuando compiten?”.
- Dificultad 7/10; costo 4/10; tiempo 5/10; usuarios 7/10; competencia 6/10; monetización 8/10; necesidad 9/10; ventaja 8/10; factibilidad 8/10.

### Solución 6 — Extensión “Claim Audit”

- Añade a posts de bots una ficha con wallet, periodo, PnL, concentración, supuestos y banderas de reproducibilidad.
- Evidencia: preguntas a Grok sobre si es real y acusaciones de promoción.
- Dificultad 5/10; costo 3/10; tiempo 3/10; usuarios 8/10; competencia 5/10; monetización 6/10; necesidad 8/10; ventaja 8/10; factibilidad 8/10.

### Solución 7 — Curso-laboratorio de bots verificables

- Enseña mediante estrategias que pueden fallar y reportes reproducibles.
- Evidencia: petición de formación y acceso.
- Dificultad 3/10; costo 2/10; tiempo 3/10; usuarios 8/10; competencia 8/10; monetización 7/10; necesidad 8/10; ventaja 5/10; factibilidad 9/10.

### Solución 8 — Simulador de riesgo y bankroll

- Drawdown, pérdida de sesión, posiciones concurrentes, piernas sin hedge y kill switch.
- Evidencia: comentarios de capital/drawdown y motor de riesgo paper de QuantBot.
- Dificultad 5/10; costo 3/10; tiempo 3/10; usuarios 7/10; competencia 6/10; monetización 7/10; necesidad 8/10; ventaja 7/10; factibilidad 9/10.

### Solución 9 — Research-to-Content Engine

- Genera informes visuales trazables desde ejecuciones de wallet.
- Evidencia: publicación diaria de análisis y repetición del formato.
- Dificultad 4/10; costo 3/10; tiempo 3/10; usuarios 7/10; competencia 8/10; monetización 7/10; necesidad 8/10; ventaja 6/10; factibilidad 9/10.

### Solución 10 — Polymarket Builder Starter Kit seguro

- Plantilla con collector, base, paper execution, health checks y reportes.
- Evidencia: posts del Builder Program y preguntas sobre construcción.
- Dificultad 4/10; costo 3/10; tiempo 3/10; usuarios 7/10; competencia 6/10; monetización 6/10; necesidad 7/10; ventaja 6/10; factibilidad 9/10.

### Solución 11 — Diario de tesis y calibración

- Registra probabilidad, evidencia, cambios y resultado de cada predicción.
- Evidencia: 82 posts de investigación/predicción.
- Dificultad 3/10; costo 2/10; tiempo 2/10; usuarios 7/10; competencia 7/10; monetización 6/10; necesidad 7/10; ventaja 5/10; factibilidad 9/10.

### Solución 12 — API paper compatible con CLOB

- Sustituye endpoint real por uno simulado.
- Evidencia: PaperMarket y necesidad de practicar.
- Dificultad 7/10; costo 5/10; tiempo 6/10; usuarios 7/10; competencia 9/10; monetización 8/10; necesidad 8/10; ventaja 4/10; factibilidad 7/10.

### Solución 13 — News-to-Market Relevance Engine

- Asocia noticias a mercados y alerta cambios de tesis.
- Evidencia: promoción de feeds y terminales de noticias.
- Dificultad 7/10; costo 5/10; tiempo 5/10; usuarios 7/10; competencia 8/10; monetización 7/10; necesidad 7/10; ventaja 5/10; factibilidad 7/10.

### Solución 14 — Portfolio tracker de estrategia

- Agrupa PnL por lógica, régimen y calidad, no solo por wallet.
- Evidencia: análisis de PnL y estrategias.
- Dificultad 4/10; costo 3/10; tiempo 4/10; usuarios 8/10; competencia 9/10; monetización 7/10; necesidad 7/10; ventaja 4/10; factibilidad 9/10.

### Solución 15 — Historical CLOB Data API

- Vende libros por tick para backtests.
- Evidencia: necesidad técnica real y proveedores actuales.
- Dificultad 10/10; costo 10/10; tiempo 10/10; usuarios 7/10; competencia 8/10; monetización 9/10; necesidad 9/10; ventaja 4/10; factibilidad 4/10.

### Solución 16 — Cross-market Arbitrage Monitor

- Normaliza contratos entre Polymarket/Kalshi/otros y detecta spreads.
- Evidencia: arbitraje recurrente y APIs cross-platform actuales.
- Dificultad 8/10; costo 7/10; tiempo 7/10; usuarios 6/10; competencia 8/10; monetización 8/10; necesidad 7/10; ventaja 5/10; factibilidad 6/10.

### Solución 17 — No-code Bot Builder

- Constructor visual de estrategias y despliegue paper.
- Evidencia: vibe coding y deseo de obtener bots.
- Dificultad 8/10; costo 7/10; tiempo 8/10; usuarios 9/10; competencia 9/10; monetización 8/10; necesidad 8/10; ventaja 4/10; factibilidad 6/10.

## 8. Matriz de decisión ponderada

Ponderación aplicada: necesidad 20%, factibilidad técnica 15%, costo favorable 10%, velocidad MVP 10%, acceso a usuarios 15%, monetización 15%, diferenciación 10% y escalabilidad 5%. “Costo favorable” puntúa alto cuando es barato.

| Pos. | Solución | Necesidad | Fact. | Costo | Velocidad | Usuarios | Monet. | Dif. | Escala | Total /100 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | Bot Truth Lab | 10 | 8 | 7 | 8 | 9 | 9 | 9 | 9 | **87,5** |
| 2 | Strategy-to-Spec Copilot | 9 | 8 | 9 | 9 | 9 | 8 | 6 | 9 | **84,0** |
| 3 | Wallet Copyability Audit | 9 | 8 | 8 | 8 | 8 | 8 | 6 | 8 | **80,0** |
| 4 | Insider Radar multiseñal | 9 | 8 | 7 | 8 | 8 | 8 | 5 | 8 | **79,0** |
| 5 | Edge Decay Monitor | 9 | 7 | 7 | 7 | 7 | 8 | 8 | 8 | **76,5** |
| 6 | Extensión Claim Audit | 8 | 8 | 8 | 8 | 8 | 6 | 8 | 8 | **75,0** |
| 7 | Curso-laboratorio | 8 | 9 | 9 | 9 | 8 | 7 | 4 | 8 | **78,0** |
| 8 | Simulador de riesgo | 8 | 9 | 9 | 9 | 7 | 7 | 6 | 9 | **75,5** |
| 9 | Research-to-Content Engine | 8 | 9 | 9 | 9 | 7 | 7 | 5 | 9 | **74,5** |
| 10 | Builder Starter Kit | 7 | 9 | 9 | 9 | 7 | 6 | 5 | 9 | **71,5** |
| 11 | Diario de tesis | 7 | 9 | 9 | 9 | 7 | 6 | 5 | 9 | **71,5** |
| 12 | API paper compatible CLOB | 8 | 7 | 6 | 6 | 7 | 8 | 4 | 8 | **68,5** |
| 13 | News-to-Market Engine | 7 | 7 | 6 | 6 | 7 | 7 | 5 | 8 | **66,5** |
| 14 | Portfolio tracker | 7 | 9 | 8 | 8 | 8 | 7 | 3 | 9 | **73,5** |
| 15 | Historical CLOB Data API | 9 | 4 | 2 | 2 | 7 | 9 | 4 | 9 | **63,0** |
| 16 | Cross-market Arbitrage | 7 | 6 | 5 | 5 | 6 | 8 | 5 | 8 | **63,0** |
| 17 | No-code Bot Builder | 8 | 5 | 4 | 4 | 9 | 8 | 4 | 9 | **65,0** |

La posición numérica sigue el total, pero las soluciones 2–5 se consideran módulos potenciales del ganador. No conviene construir cinco productos separados.

## 9. Top 5

### 1. Bot Truth Lab

1. **Problema:** falta una forma reproducible de decidir si un bot público tiene edge copiable.
2. **Posts:** serie de bots mayo–agosto, artículo de cinco bots, post fijado y PaperMarket.
3. **Comentarios:** reproducción, credibilidad, latencia, fees, drawdown y edge decay.
4. **Frecuencia:** aproximadamente 125 posts de bots y decenas de comentarios técnicos.
5. **Usuario:** builder, cuant, creador de research o trader avanzado.
6. **Funcionamiento:** ingestión → spec → datos → sim → forward → evidencia.
7. **Funciones:** provenance, runner, ejecución paper, gates, comparación con baseline, informe.
8. **Monetización:** informes, suscripción y API.
9. **Competidores:** DepthFeed, papermarket.dev, PolySimulator y herramientas internas.
10. **Diferencia:** parte de una afirmación/wallet y termina en falsación contemporánea.
11. **Dificultad:** 7/10.
12. **Tecnología:** Python existente, SQLite/Postgres, CLOB/RTDS, FastAPI y web mínima.
13. **MVP:** USD 500–2.500 incremental con trabajo propio; USD 5.000–15.000 si se contrata.
14. **Primero:** tres informes reproducibles, sin UI multiusuario.
15. **Riesgo:** competir con DepthFeed y no conseguir datos suficientes para HFT.
16. **Por qué #1:** mayor reutilización de activos y problema más repetido.

### 2. Strategy-to-Spec Copilot

Convierte lenguaje natural en un protocolo explícito, pero exige confirmación humana. Es rápido y valioso; sin runner sería solo otro wrapper de IA. Debe nacer como módulo del ganador. MVP: extractor de universo, señales, timing, order type, sizing, exits, unknowns y tests. Costo propio: USD 200–1.000. Riesgo: generar falsa precisión a partir de información incompleta.

### 3. Wallet Copyability Audit

Responde cuánto PnL habría sido alcanzable con una cuenta concreta. Funciones: reconstrucción, delay configurable, slippage, capital, límite de participación, partial fills y diferencia entre maker/taker. Monetización por wallet o suscripción. Competidores analizan wallets, pero pocos hacen la pregunta de copiabilidad. Riesgo: sin order-book histórico, algunas estimaciones solo pueden ser cotas.

### 4. Insider Radar multiseñal

Tiene la evidencia explícita más fuerte: dashboard, alertas y cuatro pasos. Funciones: wallet age, first market, concentración, size, relación temporal, clusters y confirmación externa. Monetización USD 19–79/mes. Competidores: PolyMate, PolyWallet, SharpTrack, PolyBot, Polyfollow e Insiders.Now. Está en el top por demanda, pero no gana por saturación y riesgo de etiquetar erróneamente.

### 5. Edge Decay Monitor

Ejecuta ventanas sucesivas y avisa cuando cae frecuencia, fill rate, edge neto o estabilidad. Se apoya directamente en la política de 24 horas y el forward de QuantBot. Es defendible, pero demasiado estrecho como producto inicial independiente; funciona mejor como razón para mantener la suscripción de Bot Truth Lab.

## 10. Ganador absoluto — Bot Truth Lab

### 1. La solución

Un laboratorio que convierte afirmaciones de estrategias de Polymarket en experimentos paper reproducibles y reportes de evidencia.

### 2. El problema

Los usuarios ven una wallet o un post rentable, pero no pueden saber si:

- la explicación coincide con las transacciones;
- las reglas están completas;
- el resultado depende de latencia/capital/rebates;
- el edge sobrevive fees, slippage y partial fills;
- la frecuencia es suficiente;
- o sigue vivo hoy.

### 3. La evidencia

- [Post fijado](https://x.com/RetroValix/status/2032506967923515706): 740.000+ vistas y 111 respuestas, pero muchas objeciones de realidad y reproducibilidad.
- [Bot de $81.000](https://x.com/RetroValix/status/2057127796443377791): el autor reconoce que construir es fácil y hacer funcionar exige testing/fixing.
- [Artículo de cinco bots](https://x.com/RetroValix/status/2087291446357348440): el análisis de más de un millón de ejecuciones demuestra un flujo repetitivo automatizable.
- [PaperMarket](https://x.com/RetroValix/status/2003545992101089433): prueba que existe necesidad de práctica sin dinero, pero el dominio actual está suspendido.
- QuantBot: V0.13/V0.14/V0.16/V0.17 muestran por qué un laboratorio debe reportar fallos, no fabricar candidatos.

### 4. El patrón

Un post explica; otro muestra PnL; un comentario pide el bot; otro cuestiona fees; otro pregunta por drawdown; otro advierte latencia. PaperMarket permite practicar, pero no demuestra que la estrategia del post sea reproducible. QuantBot sí tiene la disciplina de datos y gates. Juntos forman una necesidad mayor: **evidencia ejecutable entre contenido y capital**.

### 5. Usuario objetivo

Primario:

- builders independientes de Polymarket;
- quants que prototipan con IA;
- creadores que publican análisis de wallets;
- comunidades que evalúan bots/señales.

Secundario:

- traders avanzados que compran research;
- equipos de riesgo o due diligence;
- plataformas que necesitan una ficha de estrategia.

No es inicialmente para el usuario que solo quiere apostar manualmente.

### 6. MVP

**IMPRESCINDIBLE**

- entrada por wallet o plantilla de estrategia;
- spec versionada con campos desconocidos explícitos;
- provenance de datos y hashes;
- runner paper sin wallet/órdenes;
- fees, slippage, profundidad disponible y pierna incompleta;
- máximo 24 horas por experimento nuevo;
- baseline contra Polymarket/regla simple;
- gates de frecuencia, cobertura, PnL neto y estabilidad;
- informe HTML/Markdown reproducible;
- estados PASS / FAIL / INSUFFICIENT_EVIDENCE.

**IMPORTANTE**

- delayed-copy simulation;
- comparación entre capitales y latencias;
- alertas de edge decay;
- ejecución parcial y prioridad aproximada de cola;
- ficha pública compartible;
- cartera paper y kill switch.

**PARA EL FUTURO**

- copilot de lenguaje natural;
- histórico externo de order book;
- multi-market/multi-venue;
- colaboración y API;
- integración builder, siempre separada del laboratorio;
- órdenes reales solo tras otro proyecto de seguridad, no como extensión automática.

### 7. Funcionamiento

1. El usuario pega el enlace de un post o una wallet.
2. El sistema obtiene transacciones públicas y contexto del mercado.
3. Propone una spec y marca lo que no está en la evidencia.
4. El usuario confirma o corrige los supuestos.
5. El runner ejecuta la hipótesis en paper con un protocolo congelado.
6. Se registran señales, precios ejecutables, fills simulados, rechazos y salud de datos.
7. Al terminar, se leen outcomes según el contrato del experimento.
8. El reporte separa desempeño bruto, neto, frecuencia, riesgo, cobertura y sensibilidad.
9. Si falla, el fallo queda sellado; no se ajusta el umbral retrospectivamente.

### 8. Tecnología

- **Core:** Python 3.11+ y módulos existentes de QuantBot.
- **Collector:** Gamma, Data API, CLOB/RTDS y fuentes de referencia necesarias.
- **Almacenamiento MVP:** SQLite por experimento, inmutable y auditable.
- **SaaS posterior:** Postgres para usuarios/metadatos; object storage para artefactos.
- **Backend:** FastAPI.
- **Frontend:** interfaz pequeña; evitar un terminal complejo en el MVP.
- **IA:** opcional para extraer una spec y redactar; nunca decide PnL ni altera gates.
- **Jobs:** procesos reanudables/supervisor ya existente; cola distribuida solo al escalar.
- **Hosting:** una VM para MVP; workers separados por experimento al crecer.
- **Seguridad:** sin wallet, private keys ni orden real en el core.

La API oficial ya ofrece Gamma, Data y CLOB; las lecturas de mercado son públicas y sin autenticación. La documentación oficial también publica límites y SDKs. Fuentes: [introducción API](https://docs.polymarket.com/api-reference/introduction), [market data](https://docs.polymarket.com/market-data/overview), [rate limits](https://docs.polymarket.com/api-reference/rate-limits), [SDKs](https://docs.polymarket.com/api-reference/clients-sdks).

### 9. Dificultad de construcción

**7/10.**

El proyecto ya tiene collector, datasets auditables, forward shadow, gates, supervisor, políticas de duración y riesgo paper. Falta generalizar:

- estrategia específica → spec/DSL;
- una prueba → múltiples experimentos aislados;
- fills simplificados → modelo configurable;
- CLI técnico → informe consumible;
- uso personal → usuarios y cuotas.

La dificultad mayor no es la UI: es que la simulación no prometa una fidelidad que los datos no permiten.

### 10. Costo

- **MVP extremadamente básico, con trabajo propio:** USD 500–2.500 de infraestructura, diseño y servicios; 1–3 semanas.
- **MVP funcional para 10–50 usuarios:** USD 5.000–15.000 si se contrata apoyo; 4–8 semanas.
- **Producto profesional:** USD 30.000–80.000; 3–6 meses, sin incluir compra masiva de datos históricos.
- **Operación inicial:** USD 50–400/mes; puede crecer mucho si se almacenan libros completos por tick.

### 11. Tiempo

- Semana 1: contrato de estrategia y formato de evidencia.
- Semanas 2–3: tres casos reproducibles desde posts de RetroValix.
- Semanas 4–6: runner genérico, reports y validación con usuarios.
- Semanas 7–10: web mínima, pagos y aislamiento multiusuario.
- Después: partial fills avanzados, data vendors y edge decay.

### 12. Monetización

Modelo recomendado: **servicio primero, luego freemium + suscripción**.

- Informe público limitado: gratis.
- Auditoría individual: USD 49–199.
- Pro: USD 29/mes, 3 experimentos activos y reportes privados.
- Builder: USD 99/mes, 15 experimentos, exportaciones y API limitada.
- Desk: USD 299+/mes, capacidad y soporte.
- White-label para creadores/comunidades: USD 500–2.000/mes.

No cobrar por promesa de rentabilidad. Cobrar por datos, proceso, capacidad y evidencia.

### 13. Competidores

| Competidor | Qué resuelve | Ventaja actual | Hueco para Bot Truth Lab |
|---|---|---|---|
| [DepthFeed](https://polymarketbacktesting.com/) | Order book histórico, Backtest Lab, paper, robustez y wallet intelligence | Archivo profundo y producto avanzado | No competir por datos; especializarse en claim/wallet → spec → forward falsable |
| [papermarket.dev](https://papermarket.dev/) | API paper compatible con CLOB | Cambio de endpoint mínimo | Añadir investigación, gates y explicación de por qué falla |
| [PolySimulator](https://polysimulator.com/) | Paper trading manual, leaderboard, API y backtesting | Experiencia completa | Enfocarse en bots/estrategias, no práctica manual |
| [PolyMate](https://polymate.dev/) | Wallet analysis, copy trading y radar | Distribución en Telegram | Auditar copiabilidad antes de copiar |
| [PolyWallet](https://polywallet.app/) | PnL, posiciones y wallet tracking | Portfolio UX | Analizar causalidad/ejecución de una estrategia |
| [SharpTrack](https://www.sharptrack.app/) | Perfil y breakdown de estrategia | Respuesta rápida por wallet | Reproducibilidad y forward contemporáneo |
| PaperMarket de RetroValix | Simulador con USDC virtual | Ajuste directo a audiencia | Dominio suspendido; faltó workflow de validación y distribución |
| APIs oficiales | Datos y ejecución | Fuente primaria | No empaquetan una auditoría reproducible |

Si DepthFeed ya satisface a los usuarios objetivo con menor costo, se debe integrar o revender su data, no reconstruir su archivo. Esa es una condición explícita para continuar.

### 14. Validación barata

Antes de construir SaaS:

1. Elegir tres posts de RetroValix: market making, directional hedge e inventario UP/DOWN.
2. Publicar tres “evidence packs” con la metodología de QuantBot.
3. Crear una página simple con ejemplos y botón “auditar mi wallet/estrategia”.
4. Entrevistar 20 usuarios que comentaron preguntas técnicas.
5. Pedir un depósito reembolsable de USD 19 o una auditoría de USD 49.

Gate de validación:

- al menos 10 solicitudes calificadas;
- 5 pagos en 30 días;
- 3 usuarios que entreguen una segunda estrategia;
- al menos 50% de los usuarios entiende y valora un FAIL.

Si solo pagan cuando el informe promete beneficio, **no construir el SaaS**; el incentivo estaría roto.

### 15. Primeros usuarios

- **10:** personas que preguntaron “cómo reproducir”, builders conocidos y RetroValix como usuario de diseño.
- **100:** serie pública semanal “Bot Claim vs Evidence”, colaboración con comunidades y reportes compartibles.
- **1.000:** API/white-label, directorio público de estrategias, referrals y alianzas con proveedores de datos/builders.

No se debe automatizar spam ni usar datos personales no necesarios.

### 16. Riesgos

- **Técnico:** datos incompletos y ejecución imposible de reconstruir exactamente.
- **Comercial:** usuarios prefieren señales excitantes a auditorías honestas.
- **Legal/reputacional:** lenguaje de inversión, promesas, acusaciones de insider y uso de marca.
- **API:** cambios, rate limits o nuevas versiones del CLOB.
- **Competencia:** DepthFeed puede añadir el mismo workflow.
- **Copia:** la interfaz y los informes son copiables.
- **Costo:** almacenar full depth por tick escala rápidamente.
- **Escalabilidad:** cada experimento consume streams, CPU, disco y revisión.

Las reglas de integridad de Polymarket prohíben manipulación, front-running e información indebida; el producto debe hablar de “wallets anómalas” y evidencia, no afirmar delitos. Fuente: [Polymarket Market Integrity](https://integrity.polymarket.com/).

### 17. Potencial

- Necesidad real: 10/10.
- Facilidad de creación: 8/10 gracias a QuantBot.
- Facilidad de venta: 7/10.
- Monetización: 9/10.
- Escalabilidad: 8/10.
- Competencia: 6/10 (10 sería poca competencia).
- Ventaja competitiva: 9/10 si se conserva disciplina fail-closed.
- Potencial global: 8,5/10.

### 18. Veredicto final

**Sí, lo construiría**, pero en dos condiciones:

1. empezar como tres informes pagables y un runner interno, no como SaaS completo;
2. no abrir wallet, órdenes ni dinero real, ni competir por un histórico de order book que otro proveedor ya tiene.

El activo diferenciador no es “usar IA” ni “tener un dashboard”. Es demostrar de forma reproducible por qué una estrategia merece seguir o debe cerrarse.

## 11. Oportunidad oculta

# OPORTUNIDAD OCULTA — Evidence-as-Content para creadores

La oportunidad que un análisis superficial pasaría por alto es vender primero el laboratorio como **servicio white-label de investigación verificable para creadores de contenido, comunidades y builders**.

RetroValix ya posee distribución, repite el mismo análisis y recibe objeciones de credibilidad. En vez de pedirle que use un SaaS técnico, QuantBot puede entregar semanalmente:

- ficha de wallet;
- reconstrucción de estrategia;
- supuestos desconocidos;
- prueba forward paper;
- video/chart reproducible;
- resumen para X;
- y sello PASS/FAIL/INSUFFICIENT EVIDENCE.

Esto es más fácil de vender que una plataforma nueva, genera casos reales y evita el error de PaperMarket: construir una interfaz antes de probar retención. Si cinco creadores pagan USD 500/mes, el negocio valida USD 2.500 MRR con muy pocos usuarios. Después se automatizan las partes repetitivas y se abre el producto.

El riesgo es editorial: un creador puede rechazar resultados negativos. Por eso el contrato debe impedir comprar un resultado; solo se compra el análisis.

## Resultado final obligatorio

**PUBLICACIONES ANALIZADAS:** 488

**COMENTARIOS/RESPUESTAS ANALIZADOS:** 245 aproximadamente

**PROBLEMAS DIFERENTES DETECTADOS:** 18

**OPORTUNIDADES GENERADAS:** 17

**TOP 3:**
1. Bot Truth Lab — laboratorio de verificación reproducible de estrategias.
2. Strategy-to-Spec Copilot — de post narrativo a protocolo comprobable.
3. Wallet Copyability Audit — estimación realista de PnL copiable.

**GANADOR ABSOLUTO:**  
Bot Truth Lab

**PROBLEMA QUE RESUELVE:**  
Determina si una estrategia o bot público es reproducible y conserva edge bajo ejecución paper contemporánea, costos y controles explícitos.

**DIFICULTAD DE CREACIÓN:**  
7/10

**POTENCIAL ECONÓMICO:**  
9/10

**FACTIBILIDAD:**  
9/10

**INVERSIÓN INICIAL ESTIMADA:**  
USD 500–2.500 para un MVP interno con trabajo propio; USD 5.000–15.000 para un MVP funcional contratado.

**MOTIVO PRINCIPAL PARA CONSTRUIRLO:**  
El problema aparece de forma repetida en posts y comentarios, y QuantBot ya contiene la mayor parte de la infraestructura difícil: datos auditables, forward paper, gates y seguridad fail-closed.

**MAYOR RIESGO:**  
Que los usuarios prefieran promesas de rentabilidad y que competidores con mejores archivos de order book absorban el workflow de verificación.

**VEREDICTO:**  
CONSTRUIR
