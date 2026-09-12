from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "love_u_4ever_research"
SOURCE = DATA_DIR / "comments_export.json"


THREADS = {
    "2055203715615981660": ("Límites de API", "api_reliability"),
    "2058993839055224971": ("Resolución CTF/UMA", "settlement_rules"),
    "2064874631652409661": ("Fallo de mapeo MLB", "market_semantics"),
    "2079606384187126117": ("Relayer, retiros y settlement", "api_reliability"),
    "2082410524839686547": ("Rebates y rolling WV", "fees_rewards"),
    "1936360662311358496": ("Market making y rewards", "execution_liquidity"),
    "1935256339644821568": ("ETH horario y Kelly", "arbitrage_pricing"),
    "1933066221752365181": ("Acumulación sin impacto", "execution_liquidity"),
    "1933069864744464773": ("Maker/taker y salida", "execution_liquidity"),
    "1979976303270052212": ("Riesgo de copiar bots", "copy_forensics"),
    "1978561802444108283": ("Terminal para prediction markets", "terminal_demand"),
    "1974389764234366984": ("AMM frente a order book", "execution_liquidity"),
    "2089022589490565575": ("Carga mental y motivación", "focus_energy"),
    "2053498961487663541": ("Atención y concentración", "focus_energy"),
    "2083970692975083583": ("Dificultad operativa en deportes", "sports_operations"),
    "2060138249562284205": ("Reglas de cancelación y valor esperado", "settlement_rules"),
    "1978533711378288816": ("Reglas, Discord y clarificaciones", "settlement_rules"),
}


HIGH_SIGNAL_PATTERNS = [
    r"\?", r"how", r"what", r"why", r"can you", r"could", r"need", r"problem",
    r"issue", r"same issue", r"can't", r"cannot", r"doesn.t work", r"failed", r"loss",
    r"fee", r"liquidity", r"api", r"websocket", r"cancel", r"withdraw", r"settle",
    r"rebate", r"real.?time", r"bot", r"terminal", r"strategy", r"price", r"odds",
    r"怎么办", r"怎么", r"为什么", r"能不能", r"工具", r"网站", r"接口", r"失败",
    r"问题", r"手续费", r"流动性", r"提现", r"结算", r"实时", r"难算", r"亏",
]


def status_url(links: list[str]) -> str:
    for link in links:
        if re.search(r"/status/\d+$", link):
            return link
    return ""


def clean_text(raw: str) -> str:
    lines = [line.strip() for line in raw.replace("\u00a0", " ").splitlines()]
    drop = {
        "translated from chinese", "translated from japanese", "show original",
        "show translation", "rate this translation:", "relevant", "view quotes",
    }
    kept: list[str] = []
    for line in lines:
        if not line or line.lower() in drop:
            continue
        if re.fullmatch(r"[\d.,]+(?:K|M|mil)?", line, re.I):
            continue
        if re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}(?:, \d{4})?", line):
            continue
        if re.fullmatch(r"\d{1,2}:\d{2} [AP]M · .+", line):
            continue
        if line == "Views":
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def extract_identity(text: str) -> tuple[str, str]:
    lines = [line for line in text.splitlines() if line]
    display = lines[0] if lines else ""
    handle = next((line for line in lines[:4] if line.startswith("@")), "")
    return display, handle


def relevant(text: str, is_author: bool) -> bool:
    lowered = text.lower()
    return is_author or any(re.search(pattern, lowered, re.I) for pattern in HIGH_SIGNAL_PATTERNS)


def assessment(topic: str, text: str, is_author: bool) -> tuple[str, str, str, str, int]:
    lowered = text.lower()
    problem = "No formula un problema; reacción o contexto social."
    need = "No identificada."
    objection = "Ninguna explícita."
    solution = "Ninguna explícita."
    opportunity = 2

    if topic == "api_reliability":
        problem = "Operaciones degradadas o bloqueadas por API, relayer o estado técnico."
        need = "Diagnóstico visible, alertas y rutas de recuperación seguras."
        opportunity = 8
    elif topic == "settlement_rules":
        problem = "Reglas, resolución o liquidación requieren vigilancia y trabajo manual."
        need = "Preflight de reglas y seguimiento automatizado de resolución/redención."
        opportunity = 8
    elif topic == "market_semantics":
        problem = "La interfaz puede representar un contrato distinto al que se intenta comprar."
        need = "Validación independiente de token, mercado y ticket antes de firmar."
        opportunity = 10
    elif topic == "fees_rewards":
        problem = "Rebates, volumen ponderado o niveles son difíciles de calcular a tiempo."
        need = "Rentabilidad neta y progreso de tier en tiempo real."
        opportunity = 9
    elif topic == "terminal_demand":
        problem = "Datos y acciones de trading están fragmentados entre pantallas y APIs."
        need = "Terminal rápida con trading, cancelación, seguimiento y datos consolidados."
        opportunity = 9
    elif topic == "execution_liquidity":
        problem = "Profundidad, fills y salida limitan el tamaño realmente ejecutable."
        need = "Simulación de profundidad, órdenes maker y plan de salida/hedge."
        opportunity = 8
    elif topic == "arbitrage_pricing":
        problem = "La probabilidad estimada no basta: precio, liquidez y tamaño alteran el resultado."
        need = "Sizing y valor esperado con odds y profundidad ejecutables."
        opportunity = 8
    elif topic == "copy_forensics":
        problem = "Copiar fills aislados no reconstruye una estrategia de arbitraje o market making."
        need = "Forensia de canastas, inventario, hedge y causalidad."
        opportunity = 8
    elif topic == "sports_operations":
        problem = "Fees, rachas, reglas y eventos deportivos producen pérdidas difíciles de atribuir."
        need = "Atribución de P&L y controles de riesgo específicos de deportes."
        opportunity = 7
    elif topic == "focus_energy":
        problem = "Vigilancia y tareas repetitivas aumentan carga cognitiva; no constituye diagnóstico médico."
        need = "Alertas priorizadas y menos cambios de contexto."
        opportunity = 5

    if any(token in lowered for token in ("too", "hard", "difficult", "难", "不太行", "不够", "亏", "loss", "rekt")):
        objection = "La solución/estrategia actual es difícil, insuficiente o produjo una pérdida."
    if any(token in lowered for token in ("official wss", "rest api", "limit order", "kelly", "discord", "maker", "对冲", "限价", "官方data api")):
        solution = "Propone o usa una herramienta/técnica concreta mencionada en el comentario."
    if is_author and problem.startswith("No formula"):
        opportunity = 3
    return problem, need, objection, solution, opportunity


def main() -> None:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    seen: set[str] = set()

    for thread in payload:
        target = thread["target"]
        target_id = target.rstrip("/").rsplit("/", 1)[-1]
        title, topic = THREADS[target_id]
        for article in thread["articles"]:
            url = status_url(article.get("links", []))
            if not url or url == target or url in seen:
                continue
            seen.add(url)
            text = clean_text(article["text"])
            display, handle = extract_identity(text)
            is_author = "/love_u_4ever/status/" in url
            is_relevant = relevant(text, is_author)
            problem, need, objection, solution, opportunity = assessment(topic, text, is_author)
            rows.append({
                "thread_id": target_id,
                "thread_title": title,
                "thread_url": target,
                "comment_url": url,
                "display_name": display,
                "handle": handle,
                "author_followup": "Sí" if is_author else "No",
                "relevant": "Sí" if is_relevant else "No",
                "comment_summary": re.sub(r"\s+", " ", text)[:500],
                "problem": problem if is_relevant else "No se infiere un problema comercial.",
                "need": need if is_relevant else "No identificada.",
                "objection": objection,
                "proposed_solution": solution,
                "repeated_problem": "Sí" if topic in {"api_reliability", "settlement_rules", "execution_liquidity", "terminal_demand", "sports_operations"} else "No concluyente",
                "opportunity_0_10": opportunity if is_relevant else 2,
            })

    with (DATA_DIR / "comments_audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["thread_id"] for row in rows)
    relevant_rows = [row for row in rows if row["relevant"] == "Sí"]
    metrics = {
        "threads_reviewed": len(payload),
        "comments_and_responses_reviewed": len(rows),
        "third_party_comments": sum(row["author_followup"] == "No" for row in rows),
        "author_followups": sum(row["author_followup"] == "Sí" for row in rows),
        "relevant_comments_and_responses": len(relevant_rows),
        "comments_per_thread": dict(counts),
        "scope_note": "Revisión dirigida de conversaciones bajo 17 publicaciones de máxima relevancia; no representa todos los comentarios del perfil.",
    }
    (DATA_DIR / "comments_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = [
        "# Auditoría individual de comentarios y respuestas relevantes",
        "",
        f"Se revisaron {len(rows)} respuestas únicas bajo {len(payload)} publicaciones prioritarias: "
        f"{metrics['third_party_comments']} de terceros y {metrics['author_followups']} ampliaciones del autor. "
        "La selección es dirigida por relevancia y no pretende ser el universo completo de comentarios del perfil.",
        "",
    ]
    for index, row in enumerate(relevant_rows, start=1):
        md.extend([
            f"## COMENTARIO RELEVANTE #{index}",
            "",
            f"**Hilo:** {row['thread_title']} — {row['thread_url']}",
            "",
            f"**Comentario:** {row['comment_url']}",
            "",
            f"**Autor:** {row['display_name']} {row['handle']}",
            "",
            f"**Qué dice:** {row['comment_summary']}",
            "",
            f"**Qué problema menciona:** {row['problem']}",
            "",
            f"**Qué pide o desea:** {row['need']}",
            "",
            f"**Qué objeción presenta:** {row['objection']}",
            "",
            f"**Qué solución propone:** {row['proposed_solution']}",
            "",
            f"**Se repite este problema:** {row['repeated_problem']}",
            "",
            f"**Nivel de oportunidad:** {row['opportunity_0_10']}/10",
            "",
        ])
    (DATA_DIR / "comments_audit.md").write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    main()
