from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "love_u_4ever_research"
SOURCE = DATA_DIR / "x_export.json"

TWITTER_EPOCH_MS = 1_288_834_974_657


PROBLEMS = {
    "api_reliability": {
        "label": "Fiabilidad, rate limits y observabilidad de APIs",
        "patterns": [r"rate.?limit", r"\b429\b", r"api", r"websocket", r"relayer", r"nonce", r"outage", r"宕机", r"接口", r"限流", r"失败"],
        "need": "Monitorización unificada, control de cuotas, reintentos seguros y diagnóstico operativo.",
        "opportunity": "Consola de salud y ejecución para APIs de mercados de predicción.",
        "feasibility": "Muy alta",
        "economic": "Alto",
    },
    "execution_liquidity": {
        "label": "Ejecución, liquidez e inventario",
        "patterns": [r"maker", r"taker", r"order", r"liquidity", r"spread", r"fill", r"slippage", r"盘口", r"挂单", r"吃单", r"流动性", r"成交", r"撤单", r"深度", r"仓位"],
        "need": "Calcular precio ejecutable, coordinar patas y evitar inventario residual o impacto de mercado.",
        "opportunity": "Motor de ejecución y canastas atómicas para Polymarket.",
        "feasibility": "Alta",
        "economic": "Muy alto",
    },
    "arbitrage_pricing": {
        "label": "Detección de arbitraje y errores de precio",
        "patterns": [r"arbitr", r"arb\b", r"mispric", r"pricing", r"correlation", r"hedg", r"套利", r"搬砖", r"定价", r"对冲", r"价差", r"概率"],
        "need": "Comparar mercados, resultados y sedes con profundidad y costes reales.",
        "opportunity": "Escáner ejecutable de arbitraje intraevento y entre plataformas.",
        "feasibility": "Alta",
        "economic": "Muy alto",
    },
    "fees_rewards": {
        "label": "Comisiones, rebates y recompensas",
        "patterns": [r"fee", r"rebate", r"reward", r"tier", r"volume", r"airdrop", r"手续费", r"返佣", r"奖励", r"积分", r"刷量", r"撸毛"],
        "need": "Atribuir costes e incentivos por estrategia y anticipar cambios de rentabilidad.",
        "opportunity": "Analizador de rentabilidad neta, tiers y recompensas.",
        "feasibility": "Muy alta",
        "economic": "Alto",
    },
    "settlement_rules": {
        "label": "Resolución, reglas, redención y capital bloqueado",
        "patterns": [r"settle", r"resolv", r"redeem", r"claim", r"uma", r"ctf", r"clarification", r"challenge", r"merge", r"convert", r"结算", r"赎回", r"规则", r"到期", r"合并", r"转换"],
        "need": "Saber cuándo y cómo se resolverá cada mercado y automatizar acciones posteriores.",
        "opportunity": "Sentinel de reglas, resolución y recuperación de capital.",
        "feasibility": "Muy alta",
        "economic": "Alto",
    },
    "wallet_safety": {
        "label": "Seguridad de cartera, red y transacciones",
        "patterns": [r"wallet", r"recovery", r"wrong network", r"sandwich", r"mev", r"hack", r"scam", r"phishing", r"钱包", r"转错", r"救援", r"夹子", r"黑客", r"诈骗", r"貔貅"],
        "need": "Prevenir transferencias erróneas, tokens maliciosos y ejecuciones vulnerables.",
        "opportunity": "Capa de prevalidación y recuperación para traders on-chain.",
        "feasibility": "Media",
        "economic": "Alto",
    },
    "copy_forensics": {
        "label": "Forensia de traders y riesgo de copy trading",
        "patterns": [r"copy.?trad", r"leaderboard", r"watchlist", r"whale", r"insider", r"account", r"profile", r"跟单", r"排行榜", r"巨鲸", r"账户", r"地址"],
        "need": "Distinguir señal real, estructura de canasta y actividad engañosa antes de copiar.",
        "opportunity": "Reconstrucción de estrategias de carteras, no simple copia de fills.",
        "feasibility": "Alta",
        "economic": "Alto",
    },
    "market_semantics": {
        "label": "Errores de interfaz y semántica de mercados",
        "patterns": [r"frontend", r"button", r"color", r"wrong", r"bug", r"market creation", r"规则", r"前端", r"按钮", r"颜色", r"错误", r"市场创建"],
        "need": "Validar que interfaz, tokens, reglas y órdenes representan el mismo contrato económico.",
        "opportunity": "Validador independiente de mercados y tickets antes de operar.",
        "feasibility": "Alta",
        "economic": "Medio",
    },
    "sports_operations": {
        "label": "Riesgo operativo y modelado en deportes",
        "patterns": [r"sports?", r"nba", r"mlb", r"football", r"soccer", r"world cup", r"game", r"match", r"体育", r"比赛", r"世界杯", r"篮球", r"棒球"],
        "need": "Incorporar horarios, cancelaciones, alineaciones y reglas específicas antes de cotizar.",
        "opportunity": "Asistente de riesgo deportivo para prediction markets.",
        "feasibility": "Media",
        "economic": "Alto",
    },
    "cross_venue": {
        "label": "Fragmentación entre Polymarket, Kalshi y otros venues",
        "patterns": [r"kalshi", r"hyperliquid", r"hip-?4", r"poly.?perps", r"venue", r"exchange", r"交易所", r"跨平台"],
        "need": "Unificar mercados equivalentes, reglas, precios, comisiones y estado técnico.",
        "opportunity": "Terminal multivenue de comparación y arbitraje.",
        "feasibility": "Media",
        "economic": "Muy alto",
    },
    "ai_productivity": {
        "label": "IA para desarrollo y automatización",
        "patterns": [r"codex", r"chatgpt", r"claude", r"\bai\b", r"agent", r"vibe", r"人工智能", r"模型"],
        "need": "Traducir intención de trading a herramientas verificables con menos trabajo manual.",
        "opportunity": "Copiloto técnico para diseñar, probar y operar estrategias.",
        "feasibility": "Alta",
        "economic": "Alto",
    },
    "focus_energy": {
        "label": "Concentración, energía y carga mental",
        "patterns": [r"adhd", r"focus", r"energy", r"tired", r"attention", r"注意力", r"精力", r"疲", r"做不完", r"没兴趣"],
        "need": "Reducir vigilancia manual, cambios de contexto y tareas repetitivas.",
        "opportunity": "Alertas accionables y automatización fail-closed.",
        "feasibility": "Alta",
        "economic": "Medio",
    },
    "airdrop_discovery": {
        "label": "Descubrimiento y gestión de airdrops",
        "patterns": [r"airdrop", r"points", r"campaign", r"farm", r"空投", r"撸毛", r"积分", r"任务", r"白单"],
        "need": "Filtrar oportunidades, seguir requisitos y controlar exposición a estafas.",
        "opportunity": "Tracker de campañas y riesgo de airdrops.",
        "feasibility": "Alta",
        "economic": "Medio",
    },
}


def tweet_datetime(url: str) -> datetime:
    tweet_id = int(url.rstrip("/").rsplit("/", 1)[-1])
    milliseconds = (tweet_id >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


METRIC_RE = re.compile(r"^(?:[\d.,]+(?:\s?(?:K|M|mil))?|[\d.,]+\s*mil)$", re.I)


def clean_own_text(raw: str) -> tuple[str, str]:
    lines = [line.strip() for line in raw.replace("\u00a0", " ").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if len(lines) >= 4 and lines[1].startswith("@love_u_4ever"):
        lines = lines[4:]
    while lines and (not lines[-1] or METRIC_RE.match(lines[-1])):
        lines.pop()
    lines = [
        line
        for line in lines
        if line.lower()
        not in {
            "translated from chinese",
            "traducido del chino",
            "show original",
            "mostrar original",
            "show more",
            "mostrar más",
        }
    ]
    text = "\n".join(lines).strip()
    own, context = text, ""
    for marker in ("\nQuote\n", "\nCita\n"):
        if marker in text:
            own, context = text.split(marker, 1)
            break
    return own.strip(), context.strip()


def clean_repost_text(raw: str) -> str:
    lines = [line.strip() for line in raw.replace("\u00a0", " ").splitlines()]
    if lines and "reposted" in lines[0].lower():
        lines = lines[4:]
    while lines and (not lines[-1] or METRIC_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).strip()


def external_links(row: dict) -> list[str]:
    links: list[str] = []
    for item in row.get("links", []):
        href = item.get("href", "")
        host = urlparse(href).netloc.lower()
        if host and host not in {"x.com", "twitter.com"} and href not in links:
            links.append(href)
    return links


def match_problems(text: str) -> list[str]:
    value = text.lower()
    found: list[str] = []
    for key, spec in PROBLEMS.items():
        if any(re.search(pattern, value, re.I) for pattern in spec["patterns"]):
            found.append(key)
    return found


def importance_score(text: str, problems: list[str], has_media: bool, links: list[str]) -> int:
    score = 1 + min(len(problems) * 2, 5)
    if len(text) >= 180:
        score += 1
    if has_media:
        score += 1
    if links:
        score += 1
    if any(token in text.lower() for token in ("need ", "problem", "issue", "why", "can we", "希望", "问题", "需要", "太高", "失败", "错误")):
        score += 1
    return min(score, 10)


def summarize(text: str, limit: int = 280) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def row_assessment(text: str, problems: list[str]) -> dict[str, str]:
    if not problems:
        return {
            "problem": "No se detecta un problema comercial claro en esta publicación.",
            "need": "No determinada.",
            "idea": "No explícita.",
            "current_solution": "No determinada.",
            "limitations": "Evidencia insuficiente para inferir una limitación.",
            "opportunity": "Sin oportunidad directa; aporta contexto conductual o temático.",
            "feasibility": "Baja",
            "economic": "Bajo",
        }
    primary = PROBLEMS[problems[0]]
    solutions = []
    lower = text.lower()
    for token in ("polymarket", "kalshi", "betmoar", "predictparity", "codex", "claude", "github", "polygonscan", "etherscan"):
        if token in lower:
            solutions.append(token)
    return {
        "problem": "; ".join(PROBLEMS[p]["label"] for p in problems[:3]),
        "need": primary["need"],
        "idea": primary["opportunity"] if any(x in lower for x in ("need", "idea", "can we", "做个", "需要", "可以")) else "No explícita; inferida del comportamiento.",
        "current_solution": ", ".join(solutions) if solutions else "Proceso propio/manual o no identificado.",
        "limitations": "La publicación señala fricción, riesgo o coste; la magnitud debe validarse con datos operativos.",
        "opportunity": primary["opportunity"],
        "feasibility": primary["feasibility"],
        "economic": primary["economic"],
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    own_rows: list[dict] = []
    topic_counts: Counter[str] = Counter()
    post_count = min(241, len(data["own"]))

    for index, row in enumerate(data["own"], start=1):
        own_text, quoted_context = clean_own_text(row["text"])
        problems = match_problems(own_text + "\n" + quoted_context)
        topic_counts.update(problems)
        media = bool(row.get("images") or row.get("videos"))
        ext_links = external_links(row)
        assessment = row_assessment(own_text + "\n" + quoted_context, problems)
        if index <= post_count:
            kind = "quote" if quoted_context else "post"
        else:
            kind = "reply"
        dt = tweet_datetime(row["url"])
        own_rows.append(
            {
                "number": index,
                "tweet_id": row["url"].rstrip("/").rsplit("/", 1)[-1],
                "date_utc": dt.isoformat(),
                "type": kind,
                "url": row["url"],
                "content_summary": summarize(own_text),
                "quoted_or_parent_context": summarize(quoted_context, 420),
                "problem_detected": assessment["problem"],
                "need_detected": assessment["need"],
                "idea_mentioned": assessment["idea"],
                "current_solution": assessment["current_solution"],
                "solution_limitations": assessment["limitations"],
                "potential_opportunity": assessment["opportunity"],
                "feasibility": assessment["feasibility"],
                "economic_potential": assessment["economic"],
                "importance_0_10": importance_score(own_text, problems, media, ext_links),
                "problem_codes": "|".join(problems),
                "has_media": media,
                "external_links": "|".join(ext_links),
                "raw_visible_text": own_text,
            }
        )

    repost_rows: list[dict] = []
    for index, row in enumerate(data["reposts"], start=1):
        text = clean_repost_text(row["text"])
        problems = match_problems(text)
        topic_counts.update(problems)
        dt = tweet_datetime(row["url"])
        repost_rows.append(
            {
                "number": index,
                "tweet_id": row["url"].rstrip("/").rsplit("/", 1)[-1],
                "original_post_date_utc": dt.isoformat(),
                "type": "repost",
                "url": row["url"],
                "content_summary": summarize(text, 420),
                "problem_codes": "|".join(problems),
                "importance_0_10": importance_score(text, problems, bool(row.get("images")), external_links(row)),
                "external_links": "|".join(external_links(row)),
                "raw_visible_text": text,
            }
        )

    own_rows.sort(key=lambda item: item["date_utc"], reverse=True)
    repost_rows.sort(key=lambda item: item["original_post_date_utc"], reverse=True)
    for index, row in enumerate(own_rows, start=1):
        row["number"] = index
    for index, row in enumerate(repost_rows, start=1):
        row["number"] = index

    write_csv(DATA_DIR / "posts_audit.csv", own_rows)
    write_csv(DATA_DIR / "reposts_audit.csv", repost_rows)

    metadata = {
        "profile": data["profile"],
        "accessible_own_posts_and_replies": len(own_rows),
        "accessible_reposts": len(repost_rows),
        "accessible_total": len(own_rows) + len(repost_rows),
        "shown_total": data["profile"]["shownPostCount"],
        "coverage_percent": round(100 * (len(own_rows) + len(repost_rows)) / data["profile"]["shownPostCount"], 2),
        "posts_or_quotes": sum(row["type"] in {"post", "quote"} for row in own_rows),
        "replies": sum(row["type"] == "reply" for row in own_rows),
        "quotes": sum(row["type"] == "quote" for row in own_rows),
        "first_accessible_own": min(row["date_utc"] for row in own_rows),
        "last_accessible_own": max(row["date_utc"] for row in own_rows),
        "problem_counts": dict(topic_counts.most_common()),
        "high_importance_8_plus": sum(int(row["importance_0_10"]) >= 8 for row in own_rows),
        "source_file": str(SOURCE),
        "method_note": "Fechas calculadas desde Snowflake IDs de X; texto y contexto proceden de cronologías visibles autenticadas.",
    }
    (DATA_DIR / "inventory_metrics.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = ["# Auditoría publicación por publicación — @love_u_4ever", ""]
    for row in own_rows:
        lines.extend(
            [
                f"## POST #{row['number']}",
                "",
                f"**Fecha:** {row['date_utc']}",
                "",
                f"**Tipo:** {row['type']}",
                "",
                f"**Enlace:** {row['url']}",
                "",
                f"**Contenido resumido:** {html.escape(row['content_summary']) or 'Contenido visual o vacío.'}",
                "",
                f"**Contexto:** {html.escape(row['quoted_or_parent_context']) or 'No visible en la tarjeta exportada.'}",
                "",
                f"**Problema detectado:** {row['problem_detected']}",
                "",
                f"**Necesidad detectada:** {row['need_detected']}",
                "",
                f"**Idea mencionada:** {row['idea_mentioned']}",
                "",
                f"**Solución que actualmente utiliza:** {row['current_solution']}",
                "",
                f"**Limitaciones de esa solución:** {row['solution_limitations']}",
                "",
                f"**Oportunidad potencial:** {row['potential_opportunity']}",
                "",
                f"**Factibilidad:** {row['feasibility']}",
                "",
                f"**Potencial económico:** {row['economic_potential']}",
                "",
                f"**Importancia para el análisis final:** {row['importance_0_10']}/10",
                "",
            ]
        )
    (DATA_DIR / "post_by_post_audit.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
