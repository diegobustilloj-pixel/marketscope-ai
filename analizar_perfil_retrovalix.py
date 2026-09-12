from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "retrovalix_posts_raw.json"
DEFAULT_OUTPUT = ROOT / "docs" / "RETROVALIX_POST_POR_POST_20260814.md"


@dataclass(frozen=True)
class Assessment:
    context: str
    problem: str
    need: str
    idea: str
    current_solution: str
    limitation: str
    opportunity: str
    feasibility: str
    economic: str
    importance: int


def _contains(text: str, *terms: str) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def _best_text(post: dict[str, Any]) -> str:
    for key in ("detailText", "text", "detailFullText", "fullText"):
        value = str(post.get(key) or "").strip()
        if value:
            return value
    return "[X no expuso texto legible en la cronología accesible.]"


def _compact(text: str, limit: int = 520) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _post_type(post: dict[str, Any], text: str) -> str:
    full = f"{post.get('fullText') or ''}\n{post.get('detailFullText') or ''}"
    labels: list[str] = []
    if post.get("pinned"):
        labels.append("post fijado")
    if post.get("paid") or _contains(full, "paid partnership"):
        labels.append("colaboración pagada")
    if _contains(full, "Quote"):
        labels.append("quote post")
    if _contains(full, "Article") or not str(post.get("text") or "").strip():
        labels.append("artículo compartido")
    if re.match(r"^\s*(?:\d+\s*/|/\s*\d+|part\s+\d+)", text, re.I):
        labels.append("hilo")
    return " / ".join(labels) if labels else "post original"


def _assess(text: str, post: dict[str, Any]) -> Assessment:
    evidence = f"{text}\n{post.get('fullText') or ''}\n{post.get('detailFullText') or ''}"

    if _contains(
        evidence,
        "trading bot",
        "hft bot",
        "market maker",
        "arbitrage bot",
        "algorithm",
        "microstructure",
    ):
        return Assessment(
            context="Análisis de un trader automatizado o de la microestructura de Polymarket.",
            problem=(
                "Una descripción narrativa del bot no basta para saber si la estrategia es "
                "reproducible ni si el PnL sobrevive a latencia, fills, comisiones y slippage."
            ),
            need=(
                "Reglas ejecutables, datos trazables y una prueba paper/forward que pueda "
                "falsar la hipótesis antes de arriesgar capital."
            ),
            idea="Descomponer el comportamiento observado en señales, inventario, cobertura y ejecución.",
            current_solution="Análisis manual de ejecuciones y explicación asistida por Claude.",
            limitation=(
                "El post no constituye por sí solo una especificación completa; normalmente faltan "
                "reglas exactas, atribución de fills y controles fuera de muestra."
            ),
            opportunity="Laboratorio de verificación reproducible de bots y estrategias públicas.",
            feasibility="Alta",
            economic="Alto",
            importance=9,
        )

    if _contains(evidence, "insider", "wallet", "copy trad", "whale"):
        return Assessment(
            context="Investigación de wallets, insiders, whales o copy trading en Polymarket.",
            problem=(
                "La actividad on-chain es pública pero está fragmentada; una wallet rentable puede "
                "ser tardía, manipulable o imposible de copiar con la misma ejecución."
            ),
            need="Filtrado, alertas y confirmación multiseñal con contexto de liquidez y replicabilidad.",
            idea="Clasificar wallets por patrón, concentración, antigüedad y comportamiento.",
            current_solution="Búsqueda manual y herramientas externas de análisis de wallets.",
            limitation="Exige varios pasos y puede confundir correlación, intención y oportunidad copiable.",
            opportunity="Radar de wallets con auditoría de copiabilidad y alertas explicables.",
            feasibility="Alta",
            economic="Alto",
            importance=8,
        )

    if _contains(evidence, "papermarket", "virtual usdc", "training simulator"):
        return Assessment(
            context="Construcción y lanzamiento de PaperMarket, un simulador de Polymarket.",
            problem="Los traders necesitan practicar una estrategia sin wallet ni dinero real.",
            need="Simulación fiel a precios, libro y ejecución, junto con un modelo sostenible de operación.",
            idea="Replicar la experiencia de Polymarket con saldo virtual.",
            current_solution="PaperMarket, creado por el propietario del perfil.",
            limitation=(
                "La publicación prueba intención y ejecución inicial, no retención ni sostenibilidad; "
                "el dominio observado el 14-08-2026 aparece suspendido."
            ),
            opportunity="Reutilizar el concepto como capa de verificación técnica, no como clon genérico.",
            feasibility="Alta",
            economic="Medio",
            importance=10,
        )

    if _contains(
        evidence,
        "terminal",
        "dashboard",
        "alert",
        "api",
        "tool",
        "platform",
        "app",
        "built with claude",
        "vibe cod",
        "builder",
        "grant",
    ):
        return Assessment(
            context="Herramienta, terminal, API, aplicación o proceso de construcción con IA.",
            problem="Los datos y flujos de trabajo están repartidos entre múltiples herramientas.",
            need="Una interfaz simple que reduzca pasos manuales sin ocultar la evidencia.",
            idea="Crear o mejorar una herramienta para investigación, automatización o ejecución.",
            current_solution="Terminales, APIs, aplicaciones de terceros y vibe coding.",
            limitation="La facilidad de construir una interfaz no garantiza datos correctos, distribución ni ventaja durable.",
            opportunity="Workbench integrado para investigación cuantitativa y generación de evidencia.",
            feasibility="Alta",
            economic="Medio",
            importance=7,
        )

    if _contains(evidence, "polymarket", "prediction market", "bet", "odds", "prediction"):
        return Assessment(
            context="Investigación, predicción o explicación de un mercado de Polymarket.",
            problem="Decidir requiere combinar probabilidades, noticias, liquidez y resolución del mercado.",
            need="Contexto verificable y seguimiento del resultado, no solo una predicción puntual.",
            idea="Convertir investigación cualitativa en una tesis medible.",
            current_solution="Análisis manual, comparación de cuotas y publicación en X.",
            limitation="La tesis puede quedar sin calibración, registro de cambios ni evaluación posterior.",
            opportunity="Diario de tesis y monitor de calibración para prediction markets.",
            feasibility="Muy alta",
            economic="Medio",
            importance=6,
        )

    if _contains(evidence, "seedance", "image", "video", "generate", "ai agent", "llm", "claude"):
        return Assessment(
            context="Uso o demostración de IA generativa y contenido multimedia.",
            problem="Crear contenido distintivo con rapidez y consistencia exige herramientas y un flujo repetible.",
            need="Automatización de investigación, guion, visualización y publicación.",
            idea="Aplicar IA generativa a contenido o prototipos.",
            current_solution="Modelos y herramientas generativas de terceros.",
            limitation="Mercado muy competido y baja diferenciación si solo se envuelve un modelo externo.",
            opportunity="Generador especializado de informes visuales de estrategias de Polymarket.",
            feasibility="Muy alta",
            economic="Medio",
            importance=5,
        )

    if _contains(evidence, "nft", "mint", "airdrop", "testnet", "node", "whitelist", "wl"):
        return Assessment(
            context="Descubrimiento y promoción de proyectos cripto/NFT en la etapa inicial del perfil.",
            problem="Detectar proyectos tempranos obliga a filtrar mucha información y riesgo promocional.",
            need="Scoring transparente, seguimiento de resultados y alertas.",
            idea="Investigar señales sociales, equipo y etapa del proyecto.",
            current_solution="Curación manual basada en actividad, seguidores y contexto cripto.",
            limitation="Las señales sociales son fáciles de manipular y la evidencia observada es histórica.",
            opportunity="Motor de due diligence con registro de predicciones; no es prioridad actual.",
            feasibility="Media",
            economic="Bajo",
            importance=2,
        )

    return Assessment(
        context="Publicación personal, comunitaria o promocional sin una necesidad técnica específica visible.",
        problem="No se observa un problema concreto en el texto accesible.",
        need="No determinada; solo aporta contexto sobre intereses, red o estilo de comunicación.",
        idea="No se identifica una idea de producto suficientemente respaldada en esta publicación aislada.",
        current_solution="No observada.",
        limitation="Evidencia insuficiente para derivar una oportunidad comercial por sí sola.",
        opportunity="Conservar como contexto y no sobreponderar en la decisión final.",
        feasibility="Baja",
        economic="Muy bajo",
        importance=1,
    )


def _date(value: Any) -> str:
    raw = str(value or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return raw or "Fecha no expuesta"


def generate(input_path: Path, output_path: Path) -> dict[str, Any]:
    posts = json.loads(input_path.read_text(encoding="utf-8"))
    posts.sort(key=lambda item: str(item.get("datetime") or ""), reverse=True)

    lines = [
        "# RetroValix — anexo de análisis post por post",
        "",
        "Fecha de corte: 14 de agosto de 2026.",
        "",
        (
            "Este anexo cubre las publicaciones originales que X expuso en la pestaña Posts. "
            "Los campos interpretativos se generan con reglas explícitas por tema y deben leerse "
            "junto con el informe principal. Cuando X recortó un texto y la vista individual no "
            "pudo cargarse, el resumen se limita literalmente al fragmento guardado. No se infiere "
            "la parte ausente."
        ),
        "",
    ]

    for index, post in enumerate(posts, start=1):
        text = _best_text(post)
        assessment = _assess(text, post)
        url = f"https://x.com/RetroValix/status/{post.get('id')}"
        lines.extend(
            [
                f"## POST #{index}",
                "",
                f"**Fecha:** {_date(post.get('datetime'))}",
                "",
                f"**Enlace:** {url}",
                "",
                f"**Tipo:** {_post_type(post, text)}",
                "",
                f"**Contenido resumido:** {_compact(text)}",
                "",
                f"**Contexto:** {assessment.context}",
                "",
                f"**Problema detectado:** {assessment.problem}",
                "",
                f"**Necesidad detectada:** {assessment.need}",
                "",
                f"**Idea mencionada:** {assessment.idea}",
                "",
                f"**Solución que actualmente utiliza:** {assessment.current_solution}",
                "",
                f"**Limitaciones de esa solución:** {assessment.limitation}",
                "",
                f"**Oportunidad potencial:** {assessment.opportunity}",
                "",
                f"**Factibilidad:** {assessment.feasibility}",
                "",
                f"**Potencial económico:** {assessment.economic}",
                "",
                f"**Importancia para el análisis final:** {assessment.importance}/10",
                "",
            ]
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "input": str(input_path),
        "output": str(output_path),
        "posts": len(posts),
        "newest": _date(posts[0].get("datetime")) if posts else None,
        "oldest": _date(posts[-1].get("datetime")) if posts else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Genera el anexo reproducible post por post de RetroValix."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = generate(args.input.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
