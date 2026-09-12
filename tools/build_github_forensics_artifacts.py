from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(r"C:\ProyectoBotV4\polymarket_quant_bot\data\github_forensics_20260911")
AS_OF = "2026-09-11"


def write_csv(name: str, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if not rows:
        raise ValueError(f"No rows for {name}")
    fields = fieldnames or list(rows[0].keys())
    with (ROOT / name).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_text(name: str, text: str) -> None:
    (ROOT / name).write_text(text.strip() + "\n", encoding="utf-8")


with (ROOT / "github_repository_inventory.json").open(encoding="utf-8-sig") as handle:
    inventory = json.load(handle)

# Replace collection-time placeholders with an explicit evidence boundary.  A Tiered
# audit is more honest than pretending all 49 histories were cloned equally deeply.
local_forensics_path = ROOT / "repository_local_forensics.json"
if local_forensics_path.exists():
    local_forensics = {
        row["name"]: row
        for row in json.loads(local_forensics_path.read_text(encoding="utf-8-sig"))
    }
else:
    local_forensics = {}

known_releases = {
    "rindexer": "latest visible GitHub release v0.43.1; 81 tags reported by API",
    "rrelayer": "latest visible GitHub release v0.14.0; 34 tags reported by API",
    "ethereum-multicall": "latest locally observed tag 2.26.0; 32 tags reported by API",
    "ethereum-abi-types-generator": "latest locally observed tag 1.3.4; 21 tags reported by API",
    "ethereum-erc20-token-balances-multicall": "latest locally observed tag 1.0.2; 3 tags reported by API",
}

for row in inventory:
    name = row["repository_name"]
    local = local_forensics.get(name)
    if local and local.get("recent_commits"):
        commits = local["recent_commits"]
        first_blob = commits[0] if isinstance(commits, list) else commits
        row["last_meaningful_commit"] = str(first_blob).splitlines()[0]
    else:
        row["last_meaningful_commit"] = (
            f"Tiered metadata review only; last_push_proxy={row.get('last_push_proxy', '')}"
        )
    tag_count = int(row.get("tag_count") or 0)
    row["releases"] = known_releases.get(
        name,
        f"{tag_count} tags observed; release page not deep-reviewed"
        if tag_count
        else "No tags/releases observed in metadata collection",
    )

write_csv("github_repository_inventory.csv", inventory, list(inventory[0].keys()))
(ROOT / "github_repository_inventory.json").write_text(
    json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

by_name = {row["repository_name"]: row for row in inventory}


# The score reflects usefulness for this Polymarket data/trading project, not general software quality.
rank_spec = {
    "rindexer": (97, "S", "Indexer EVM activo: reorgs, cursores atómicos, Postgres/GraphQL/streams y ERC-1155."),
    "rrelayer": (91, "S", "Patrones de relayer operacional: colas, nonces, KMS/HSM, allowlists, webhooks y reemplazo de gas."),
    "ethereum-multicall": (86, "S", "Patrón probado para lecturas masivas; útil para reconciliar balances, allowances y estados onchain."),
    "ethereum-bloom-filters": (82, "S", "Prefiltro de logs por bloom; acelera escaneos de bloques completos sin perder verdaderos positivos."),
    "ethereum-abi-types-generator": (79, "A", "Generación de tipos desde ABI; reutilizable como control de esquema en tiempo de build."),
    "reth-indexer": (75, "A", "Ideas de indexación directa, COPY/Parquet y bloom; el propio README lo clasifica como I+D incompleta."),
    "ethereum-erc20-token-balances-multicall": (72, "A", "Referencia para fan-out de balances; limitada a ERC-20 y dependencias antiguas."),
    "evmc": (70, "A", "Obtención de bytecode/fuente verificada y apertura en IDE; útil para inspección puntual de contratos."),
    "js-eth-personal-sign-examples": (68, "A", "Referencia compacta de firma/recuperación; solo para entender flujos heredados, no CLOB V2."),
    "simple-uniswap-sdk": (67, "A", "Patrones de rutas, cotizaciones reactivas y construcción de transacciones; integración directa no recomendada."),
    "ethers-rs": (65, "B", "Fork de biblioteca Ethereum Rust; ideas de proveedores/firma, pero no es dependencia necesaria del bot Python."),
    "reth": (64, "B", "Cliente EVM de alto rendimiento; relevante si se opera nodo propio, excesivo para la primera versión."),
    "pool-sniper": (62, "B", "Patrones de latencia y detección de pools; dominio AMM distinto y código mínimo/antiguo."),
    "defender-client": (60, "B", "Automatización operativa y relayers; fork antiguo, útil como referencia de controles."),
    "simple-pancakeswap-sdk": (58, "B", "Patrones DEX/quote, pero no se traslada al libro de órdenes de Polymarket."),
    "uniswap-dapp-integration-monorepo": (57, "B", "Arquitectura multi-framework y estado reactivo de precios; referencia UI, no motor de ejecución."),
    "vscode-solidity": (55, "B", "Herramientas Solidity y compilación; útil al auditar ABIs, no componente de producción."),
    "chains": (54, "B", "Catálogo de metadatos de redes; útil para validar chainId, RPC y exploradores."),
    "ethereum-typescript-to-solidity": (51, "B", "Experimento de generación de Solidity; ideas de codegen, alto riesgo de obsolescencia."),
    "rentable-protocol": (48, "C", "Contratos y pruebas como referencia de ingeniería; sin relación directa con CTF/CLOB."),
    "evm-cli": (46, "C", "Esqueleto de CLI EVM; proyecto WIP con superficie muy pequeña."),
    "metamask-extension": (44, "C", "Fork histórico de wallet; sirve para conceptos de firma, demasiado viejo para reutilizar."),
    "metamask-docs": (42, "C", "Documentación histórica de wallet/proveedor; poco valor frente a especificaciones actuales."),
    "vscode": (40, "C", "Fork histórico de editor; calidad alta del upstream, sin valor específico para el bot."),
    "solidity": (39, "C", "Fork histórico del compilador; sirve como contexto, no como dependencia del proyecto."),
    "web3.js": (38, "C", "Fork histórico de web3; la versión y mantenimiento no justifican integrarlo."),
    "project-layout": (37, "C", "Convenciones de repositorios Go; valor organizativo, no funcional."),
    "npm-check": (36, "C", "Higiene de dependencias JavaScript; fork antiguo y fuera del stack principal."),
    "dtslint": (35, "C", "Validación de typings; aplicable solo si se añade una capa TypeScript."),
    "express": (34, "C", "Framework web genérico; fork antiguo, usar upstream si fuese necesario."),
    "rxjs": (33, "C", "Patrones reactivos útiles para feeds, pero el fork no debe consumirse."),
    "tedious": (32, "C", "Driver SQL Server sin encaje con DuckDB/Postgres propuestos."),
    "post-robot": (31, "C", "Mensajería cross-domain de frontend; no interviene en ingesta ni trading."),
    "browser-laptop": (30, "C", "Fork histórico de Brave; sin relación práctica con Polymarket."),
    "redux-with-angular-architecture": (29, "C", "Ejemplo de estado frontend; no es prioridad para el motor cuantitativo."),
    "create-generic-complex-type-classes": (28, "C", "Utilidad de generación TypeScript pequeña y antigua."),
    "epm": (26, "D", "Experimento histórico de package manager Ethereum; no usar."),
    "epm-api": (25, "D", "Backend histórico del experimento EPM; no usar."),
    "epm-ui": (24, "D", "UI histórica del experimento EPM; no usar."),
    "bull-bear": (23, "D", "Herramienta personal sin código sustantivo para el objetivo."),
    "git-smart": (22, "D", "Idea/prototipo sin utilidad para Polymarket."),
    "open-source-package-ideas": (21, "D", "Repositorio de ideas, no componente de software."),
    "sass-helper": (20, "D", "Helpers de estilos sin relación con el núcleo."),
    "node-ip-details": (19, "D", "Geolocalización IP antigua; no debe influir en trading ni identidad de wallets."),
    "insiders-config": (18, "D", "Configuración personal sin componente reutilizable."),
    "joshstevens19": (17, "D", "Repositorio de perfil; sirve solo como metadato de autor."),
    "guidelines": (16, "D", "Fork sin material específico aprovechable."),
    "jsencrypt": (15, "D", "RSA en navegador, criptografía no usada por CLOB V2/EIP-712."),
    "lodash": (14, "D", "Fork histórico genérico; usar upstream, no el fork."),
}

assert set(rank_spec) == set(by_name), (set(by_name) - set(rank_spec), set(rank_spec) - set(by_name))

ranking = []
for name, (score, tier, reason) in sorted(rank_spec.items(), key=lambda x: (-x[1][0], x[0])):
    src = by_name[name]
    status = "activo" if src["activity_level"] in {"high", "medium"} else ("baja actividad" if src["activity_level"] == "low" else "dormante")
    action = {
        "S": "adoptar patrón o evaluar como servicio/dependencia",
        "A": "prototipar en aislamiento y fijar versión/licencia",
        "B": "usar solo como referencia selectiva",
        "C": "no integrar; consultar si surge necesidad puntual",
        "D": "descartar del roadmap",
    }[tier]
    ranking.append({
        "rank": len(ranking) + 1,
        "repository_name": name,
        "tier": tier,
        "score_0_100": score,
        "polymarket_relevance": reason,
        "recommended_action": action,
        "maintenance_assessment": status,
        "fork": src["fork"],
        "license_api": src["license"],
        "last_push_proxy": src["last_push_proxy"],
        "url": src["url"],
        "as_of": AS_OF,
    })

write_csv("github_repository_ranking.csv", ranking)


contracts = [
    ("CLOB", "CTF Exchange V2", "0xE111180000d2663C0091e4f400237545B87B996B", "actual", "Órdenes CTF binarias", "Dominio EIP-712 v2"),
    ("CLOB", "Neg Risk CTF Exchange V2", "0xe2222d279d744050d28e00520010520000310F59", "actual", "Órdenes NegRisk", "Dominio EIP-712 v2"),
    ("CTF", "Conditional Tokens", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045", "actual", "ERC-1155 outcome tokens", "Fuente canónica de splits/merges/redemptions"),
    ("Combos", "PositionManager proxy", "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF", "actual", "ERC-1155 modular de posiciones", "No confundir sus token IDs con Gnosis CTF"),
    ("Combos", "BinaryModule proxy", "0x1000008dD9001B968442c1000017eaE6E0dA00Ba", "actual", "Módulo binario", "Parte de Polymarket V2/combos"),
    ("Combos", "NegRiskModule proxy", "0x200000900045e3B6259600682756002200028933", "actual", "Módulo NegRisk modular", "Parte de Polymarket V2/combos"),
    ("Combos", "CombinatorialModule proxy", "0x30000034706C7d8e12009DAB006Be20000c031A8", "actual", "Combinaciones de hasta 50 piernas según auditorías", "Token IDs estructurados"),
    ("Combos", "Exchange proxy", "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa", "actual", "Exchange del sistema modular", "No es el CTF Exchange V2 del CLOB"),
    ("Combos", "Router", "0x12121212006e4CD160D18e3f00711DA5c3372600", "actual", "Enrutamiento/bridge", "Patrón effects-first y callbacks"),
    ("Combos", "AutoRedeemer proxy", "0xa1200000d0002264C9a1698e001292D00E1b00af", "actual", "Canje automatizado", "Auditar gas y eventos vacíos"),
    ("Collateral", "pUSD proxy", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", "actual", "Colateral CLOB V2", "Respaldado por USDC"),
    ("Collateral", "CollateralOnramp", "0x93070a847efEf7F70739046A929D47a521F5B8ee", "actual", "Entrada USDC→pUSD", "Requiere approval"),
    ("Collateral", "CollateralOfframp", "0x2957922Eb93258b93368531d39fAcCA3B4dC5854", "actual", "Salida pUSD→USDC", "Registrar wrap/unwrap"),
    ("Collateral", "CtfCollateralAdapter", "0xAdA100Db00Ca00073811820692005400218FcE1f", "actual", "Puente pUSD↔CTF", "La documentación actual reemplaza direcciones antiguas de README"),
    ("Collateral", "NegRiskCtfCollateralAdapter", "0xadA2005600Dec949baf300f4C6120000bDB6eAab", "actual", "Puente pUSD↔NegRisk CTF", "La documentación actual reemplaza direcciones antiguas de README"),
    ("Wallet", "Deposit Wallet Factory", "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07", "actual", "Crea wallets deterministas", "Arquitectura beacon/passkey/ERC-1271"),
    ("Wallet", "Deposit Wallet Beacon", "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a", "actual", "Implementación actualizable", "Cambios de implementación afectan flota"),
    ("Resolution", "UMA Adapter", "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74", "actual", "Resolución UMA del CTF", "Vigilar request/dispute/settle"),
    ("Legacy", "CTF Exchange V1", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", "legado", "Órdenes V1", "No acepta órdenes de producción desde 2026-04-28"),
    ("Legacy", "NegRisk Exchange V1", "0xC5d563A36AE78145C45a50134d48A1215220f80a", "legado", "Órdenes NegRisk V1", "Solo reconstrucción histórica"),
    ("Legacy", "NegRisk Adapter V1", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", "deprecado", "Adaptador NegRisk V1", "Marcado deprecated por documentación oficial"),
]

contract_rows = [
    {"system": a, "contract": b, "address": c, "status": d, "role": e, "indexer_note": f}
    for a, b, c, d, e, f in contracts
]


event_rows = []


def event(system, contract, name, signature, status, accounting, source, confidence="exacta"):
    event_rows.append({
        "system": system,
        "contract": contract,
        "event_name": name,
        "solidity_signature": signature,
        "deployment_status": status,
        "accounting_effect": accounting,
        "source": source,
        "signature_confidence": confidence,
        "dedupe_key": "chain_id,contract_address,transaction_hash,log_index",
        "reorg_policy": "guardar block_hash; invalidar y reprocesar si cambia la cadena canónica",
    })


ctf_v2 = "https://github.com/Polymarket/ctf-exchange-v2/tree/ccc0596074f4dfd62c944fbca4de252893b82b4b"
docs_contracts = "https://docs.polymarket.com/resources/contracts"

event("CLOB V2", "CTF/NegRisk Exchange V2", "OrderFilled", "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)", "actual", "Fill individual; derivar lado, token, cash, cantidad, fee, builder y metadata", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "OrdersMatched", "OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)", "actual", "Resumen del taker; no duplicar con OrderFilled", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "FeeCharged", "FeeCharged(address,uint256)", "actual", "Costo/ingreso de fee; reconciliar contra fills", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "OrderPreapproved", "OrderPreapproved(bytes32)", "actual", "Estado de validez de firma", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "OrderPreapprovalInvalidated", "OrderPreapprovalInvalidated(bytes32)", "actual", "Invalida preaprobación", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "UserPaused", "UserPaused(address,uint256)", "actual", "Riesgo operacional; no implica cancelación onchain individual", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "UserUnpaused", "UserUnpaused(address)", "actual", "Restablece elegibilidad", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "TradingPaused", "TradingPaused(address)", "actual", "Corte global de ejecución", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "TradingUnpaused", "TradingUnpaused(address)", "actual", "Reanudación global", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "NewOperator", "NewOperator(address,address)", "actual", "Cambio de privilegios", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "RemovedOperator", "RemovedOperator(address,address)", "actual", "Cambio de privilegios", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "FeeReceiverUpdated", "FeeReceiverUpdated(address)", "actual", "Cambio de receptor de fees", ctf_v2)
event("CLOB V2", "CTF/NegRisk Exchange V2", "MaxFeeRateUpdated", "MaxFeeRateUpdated(uint256)", "actual", "Cambio de límite de fee", ctf_v2)

event("Gnosis CTF", "Conditional Tokens", "TransferSingle", "TransferSingle(address,address,address,uint256,uint256)", "actual", "Movimiento/mint/burn de un token ERC-1155", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "TransferBatch", "TransferBatch(address,address,address,uint256[],uint256[])", "actual", "Movimiento/mint/burn de múltiples tokens; expandir por índice", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "ApprovalForAll", "ApprovalForAll(address,address,bool)", "actual", "Permiso de operador, no PnL", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "ConditionPreparation", "ConditionPreparation(bytes32,address,bytes32,uint256)", "actual", "Alta de condición y cardinalidad", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "ConditionResolution", "ConditionResolution(bytes32,address,bytes32,uint256,uint256[])", "actual", "Payout vector canónico", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "PositionSplit", "PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)", "actual", "Colateral/posición a outcomes; no tratar como compra", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "PositionsMerge", "PositionsMerge(address,address,bytes32,bytes32,uint256[],uint256)", "actual", "Outcomes a colateral/posición padre; no tratar como venta", ctf_v2)
event("Gnosis CTF", "Conditional Tokens", "PayoutRedemption", "PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)", "actual", "Realización por resolución", ctf_v2)

event("NegRisk CTF", "NegRisk Adapter", "MarketPrepared", "MarketPrepared(bytes32,address,uint256,bytes)", "actual", "Alta de evento NegRisk", ctf_v2)
event("NegRisk CTF", "NegRisk Adapter", "QuestionPrepared", "QuestionPrepared(bytes32,bytes32,uint256,bytes)", "actual", "Vincula pregunta e índice dentro del evento", ctf_v2)
event("NegRisk CTF", "NegRisk Adapter", "OutcomeReported", "OutcomeReported(bytes32,bytes32,bool)", "actual", "Resultado parcial/final por pregunta", ctf_v2)
event("NegRisk CTF", "NegRisk Adapter", "PositionsConverted", "PositionsConverted(address,bytes32,uint256,uint256)", "actual", "Conversión NOs/YES; evento económico, no trade", ctf_v2)
event("NegRisk CTF", "NegRisk Adapter", "PositionSplit", "PositionSplit(address,bytes32,uint256)", "actual", "Mint de outcomes NegRisk", ctf_v2)
event("NegRisk CTF", "NegRisk Adapter", "PositionsMerge", "PositionsMerge(address,bytes32,uint256)", "actual", "Merge NegRisk", ctf_v2)
event("NegRisk CTF", "NegRisk Adapter", "PayoutRedemption", "PayoutRedemption(address,bytes32,uint256[],uint256)", "actual", "Redención NegRisk", ctf_v2)

event("Collateral", "pUSD", "Transfer", "Transfer(address,address,uint256)", "actual", "Flujo ERC-20; incluye mint/burn según from/to cero", docs_contracts)
event("Collateral", "pUSD", "Wrapped", "Wrapped(address,address,address,uint256)", "actual", "Entrada de colateral subyacente a pUSD", ctf_v2)
event("Collateral", "pUSD", "Unwrapped", "Unwrapped(address,address,address,uint256)", "actual", "Salida de pUSD a activo subyacente", ctf_v2)
event("Collateral", "pUSD", "Paused", "Paused(address)", "actual", "Activo subyacente pausado", ctf_v2)
event("Collateral", "pUSD", "Unpaused", "Unpaused(address)", "actual", "Activo subyacente reactivado", ctf_v2)

event("CLOB V1", "Exchange V1", "OrderFilled", "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)", "legado", "Fill histórico makerAsset/takerAsset; decodificador distinto de V2", "https://github.com/Polymarket/polymarket-subgraph", "exacta para V1")
event("Combos V2", "PositionManager/modules", "eventos del ABI verificado", "ABI público no disponible en el repositorio auditado al corte", "actual", "Crear decodificador separado; obtener ABI verificado de cada proxy/implementación", docs_contracts, "pendiente de ABI; no inferir firmas")

write_csv("polymarket_event_catalog.csv", event_rows)


reusable = [
    ("rindexer", "Coordinador de reorg + rollback", "patrón/servicio", "S", "MIT", "Adoptar inmediatamente en diseño", "Mantener raw logs inmutables, canonical flag y cursor por fuente; no copiar sin resolver la carrera documentada entre rollback y commit en vuelo."),
    ("rindexer", "Inserción masiva + cursor en una transacción", "patrón/código", "S", "MIT", "Reusar", "Impide avanzar cursor sin persistir eventos y evita dobles índices tras crash."),
    ("rindexer", "Factory discovery y ERC-1155 TransferBatch", "patrón/código", "S", "MIT", "Reusar", "Útil para wallets, proxies y expansión de lotes."),
    ("rindexer", "HyperSync/RPC fallback + streams", "servicio", "S", "MIT", "Evaluar sidecar", "Postgres/GraphQL/Kafka/Redis/Webhook; el bot Python puede consumir salidas sin portar Rust."),
    ("rrelayer", "Cola, nonce, reemplazo de gas y webhooks", "patrón/servicio", "S", "MIT", "Reusar para acciones onchain", "Adecuado para approvals, wraps y redemptions; no sustituye el cliente HTTP del CLOB."),
    ("rrelayer", "KMS/Turnkey/Fireblocks/PKCS11 y allowlists", "seguridad", "S", "MIT", "Reusar por diseño", "Evitar llaves calientes en archivos; limitar métodos/contratos y rotar credenciales."),
    ("ethereum-multicall", "Lecturas agregadas", "patrón", "S", "LICENSE MIT; package.json ISC", "Reimplementar con librería Python mantenida", "Reconciliar balances ERC-20/ERC-1155 y allowances por lotes; registrar inconsistencia de licencia."),
    ("ethereum-bloom-filters", "Prefiltro bloom", "biblioteca/patrón", "S", "MIT", "Usar si se escanean bloques completos", "No aporta al RPC eth_getLogs ya filtrado; confirmar falsos positivos y cero falsos negativos."),
    ("ethereum-abi-types-generator", "Tipos generados desde ABI", "build-time", "A", "MIT", "Adoptar concepto", "Generar modelos Pydantic/decodificadores y fijar hash de ABI por contrato."),
    ("ethereum-erc20-token-balances-multicall", "Fan-out de balances", "patrón", "A", "LICENSE MIT; package.json ISC", "Referencia", "Solo ERC-20 y antiguo; extender a ERC-1155 balanceOfBatch."),
    ("reth-indexer", "COPY/Parquet e indexación desde DB de nodo", "patrón I+D", "A", "MIT", "Roadmap posterior", "README advierte features faltantes y bugs; requiere co-localizar reth."),
    ("ctf-exchange-v2", "Order struct, EIP-712, interfaces y eventos", "especificación/código", "S externo", "BUSL-1.1 hasta 2030-03-27", "Usar ABI/especificación; no copiar a producción", "Licencia permite uso no productivo y cambia a MIT en 2030; revisar texto legal antes de reutilizar código."),
    ("polymarket-subgraph", "Esquema event-sourced y PnL promedio ponderado", "referencia legacy", "A externo", "LGPL-3.0", "Reescribir, no copiar ciegamente", "V1; omite casos de transferencias externas y no es un ledger forense autoritativo."),
]

write_csv(
    "polymarket_reusable_components.csv",
    [dict(zip(["repository", "component", "reuse_type", "tier", "license", "decision", "notes"], row)) for row in reusable],
)


dependencies = [
    ("Bot Python", "py-clob-client-v2", "Órdenes, cancelaciones y datos CLOB V2", "producción", "Fijar versión; no usar py-clob-client V1"),
    ("Bot Python", "WebSocket CLOB V2", "Libro, trades y estados de orden en tiempo real", "producción", "Secuencia, heartbeat, gap detection y resync REST"),
    ("Bot Python", "Polygon RPC + fallback", "Logs y reconciliación onchain", "producción", "Mínimo dos proveedores; pin por block hash"),
    ("Indexer", "rindexer o implementación equivalente", "Ingesta reorg-safe", "evaluación", "Sidecar recomendado si el volumen supera el pipeline Python"),
    ("Ledger", "DuckDB", "Raw/derived tables, backtest y análisis local", "actual recomendado", "Mantener eventos inmutables y materializaciones reconstruibles"),
    ("Ledger escalado", "PostgreSQL", "Concurrencia/serving/GraphQL", "fase 2", "Migrar solo por necesidad operativa, no por moda"),
    ("Contract schemas", "ABIs versionadas + hash", "Decodificación exacta por address/block range", "producción", "Nunca reutilizar ABI V1 en V2"),
    ("CLOB signing", "EIP-712 Exchange domain v2", "Firma de órdenes", "producción", "chain 137; verifyingContract depende de CTF vs NegRisk"),
    ("API auth", "ClobAuthDomain v1", "Autenticación L1/L2", "producción", "No confundir con versión del dominio Exchange"),
    ("Wallet execution", "session key / KMS signer", "Firmas acotadas", "opcional", "Session Keys beta: Deposit Wallet, tiempo limitado, sin withdrawals"),
    ("Onchain ops", "rrelayer patterns", "Approvals/wrap/redemption", "fase 2", "Allowlist estricta; CLOB orders siguen offchain"),
    ("Market metadata", "Gamma/Data API", "Eventos, mercados, condiciones, tokens", "producción", "Cache con timestamp y mantener historial de mutaciones"),
    ("Reconciliation", "multicall/balanceOfBatch", "Balances/allowances", "producción", "Comparar API, ledger derivado y cadena"),
    ("Observability", "structured logs + metrics + alerts", "Operación", "producción", "Alertar gaps WS, reorg, drift, stale order, saldo y exposure"),
]

write_csv(
    "polymarket_dependency_map.csv",
    [dict(zip(["component", "dependency", "purpose", "stage", "control"], row)) for row in dependencies],
)


contract_map = f"""
# Mapa de contratos Polymarket — corte {AS_OF}

## Regla de interpretación

Hay dos grafos activos distintos. El **CLOB V2** liquida mercados binarios públicos mediante Gnosis Conditional Tokens (ERC-1155) y los exchanges `0xE111…` / `0xe222…`. La familia **Combos / Polymarket V2** usa PositionManager y módulos Binary, NegRisk y Combinatorial, con otro esquema de token ID. Un ledger correcto mantiene decodificadores, tablas y namespaces separados; solo los une en una capa económica normalizada.

Fuente canónica de direcciones: [documentación oficial de contratos]({docs_contracts}). CLOB V2 está activo desde 2026-04-28: [guía de migración](https://docs.polymarket.com/v2-migration).

| Sistema | Contrato | Dirección | Estado | Función | Regla para el indexer |
|---|---|---|---|---|---|
""" + "\n".join(
    f"| {r['system']} | {r['contract']} | `{r['address']}` | {r['status']} | {r['role']} | {r['indexer_note']} |"
    for r in contract_rows
) + """

## Flujo CLOB V2 / CTF

1. Metadata identifica market/condition/token IDs.
2. Usuario firma una orden EIP-712 v2; el CLOB la almacena fuera de cadena.
3. El operador solo puede liquidar precios/cantidades autorizados por las firmas y suministra el fee dentro de límites onchain.
4. `OrderFilled` registra la ejecución. Los `TransferSingle/Batch` registran el movimiento real de ERC-1155.
5. `PositionSplit`, `PositionsMerge` y `PayoutRedemption` explican inventario que no proviene de trades.
6. El ledger reconcilia CLOB REST/WS, fills onchain, balances y flujo pUSD; ninguna fuente individual es suficiente.

## Orden firmada CLOB V2

`Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)`

- `ORDER_TYPEHASH`: `0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589`.
- `side`: BUY=0, SELL=1.
- `signatureType`: EOA=0, POLY_PROXY=1, POLY_GNOSIS_SAFE=2, POLY_1271=3.
- `timestamp` está en milisegundos y aporta unicidad; no es expiración.
- `expiration` permanece en el cuerpo HTTP para GTD, pero no forma parte de la firma V2.
- `nonce`, `feeRateBps` y `taker` no están en el struct firmado V2.
- Dominio Exchange: versión `2`; dominio de autenticación API: versión `1`.

## IDs CTF

- `conditionId = keccak256(oracle, questionId, outcomeSlotCount)` según el contrato CTF.
- `collectionId` usa la construcción criptográfica de Gnosis CTF; no debe reimplementarse a mano sin vectores oficiales.
- `positionId = keccak256(collateralToken, collectionId)` convertido a `uint256`.
- En NegRisk, `marketId`, `questionId`, índice y outcome forman relaciones adicionales; registrar siempre la procedencia.

## IDs Combos

Las auditorías describen IDs estructurados que codifican módulo, hash/base, cadena de resolución, condición y outcome. Como el repositorio `Polymarket/polymarket-v2` no fue públicamente clonable al corte, no se publica aquí un layout de bits inferido. Acción obligatoria: obtener ABI y código verificados de cada proxy/implementación y generar vectores antes de decodificar.

## Deriva detectada

El README de `contract-security` preserva direcciones de adaptadores de una revisión anterior. La página oficial actual lista `0xAdA100…` y `0xadA200…`; prevalece la documentación marcada como fuente única de verdad. El sistema debe versionar address books con `valid_from_block`, hash de ABI y fuente.
"""

write_text("polymarket_contract_map.md", contract_map)


security_findings = f"""
# Hallazgos de seguridad Polymarket — corte {AS_OF}

## Veredicto ejecutivo

La evidencia no justifica afirmar “los contratos son seguros”. Sí demuestra múltiples auditorías independientes, revisiones de diff y verificación formal; también muestra supuestos operativos y hallazgos aceptados que afectan directamente a un ledger/copy-bot. La mayoría de los hallazgos de severidad alta/media reportados fueron corregidos en los commits auditados, pero **corregido en un informe no equivale a verificar que el proxy desplegado use exactamente ese bytecode**. Esa comparación queda como control P0.

## Cobertura local

- `contract-security`: 35 PDF, 915 páginas, 1.857.085 caracteres extraídos.
- CTF Exchange V2: 2 PDF adicionales (Cantina y Quantstamp, marzo de 2026).
- Repositorios de fuente disponibles: `ctf-exchange-v2`, `rindexer`, `rrelayer`, `polymarket-subgraph` y utilidades seleccionadas.
- No accesibles públicamente al corte: `Polymarket/polymarket-v2` y `Polymarket/deposit-wallet`; se analizaron informes y direcciones, no se asumió acceso a código privado.

## Matriz resumida

| Superficie | Auditor/revisión | Conteo y severidad | Hallazgos que importan al proyecto | Estado reportado |
|---|---|---|---|---|
| Polymarket V2 núcleo | Quantstamp, mayo 2026 | 12: 1 alta, 1 media, 6 bajas, 4 info | Migración no canónica podía drenar pUSD; all-NO legado podía crear pUSD sin respaldo; eventos de batch y surplus | alta/media fixed; 2 bajas acknowledged |
| Polymarket V2 núcleo | Cantina, abril 2026 | 58: 4 medias, 19 bajas, resto gas/info | Re-request UMA sin incentivo; condiciones NegRisk fantasma; override admin; bridge preparation bloqueable | 4 medias fixed; bajas mixtas |
| Polymarket V2 núcleo | Pashov, mayo 2026 | 35: 5 medias, 30 bajas | Colateral sobrante; reward cero; pre-resolved subconditions; migración insuficiente; batch sell incorrecto | medias resueltas; bajas mixtas |
| Polymarket V2 núcleo | Sigma Prime, junio/julio 2026 | 22: 2 medias, 10 bajas, 10 info | Arbitrador defectuoso; bypass Safe multisig; redondeo; pausa/fallback; gas griefing | medias resueltas/cerradas; bajas mixtas |
| Polymarket V2 núcleo | Zellic, junio 2026 | 13: 3 medias, 5 bajas, 5 info | Configuración de request; recompensa diferida; bits sucios generaban eventos malformados; lotes vacíos | mayormente fixed; revisar accepted |
| Polymarket V2 núcleo | Certora, abril/mayo 2026 | 3 medias + 5 bajas; adicional 1 media | Refund/bridge, checks faltantes, callbacks, migración all-NO | correcciones revisadas según informes |
| Formal verification | Certora, agosto 2026 | Reglas de alta prioridad; 1 info | Riesgo de recipient no-EVM en bridge | Verifica especificaciones, no todo el sistema |
| Combinatorial | Cantina, mayo 2026 | 2 altas, 2 medias, 5 bajas | Alias de migración; overredeem; resolución antes de migración; bridge | altas/medias fixed |
| Combinatorial | Certora/Quantstamp, mayo 2026 | 1 alta, 2 medias, 2 bajas, 2 info; QSP 1 info | Invariante principal, DoS temporal y cross-chain | fix review + QSP fixed |
| CCIP/collateral return | Cantina/Certora, julio 2026 | 2 medias y bajas; Certora 2 bajas | Recipient no canónico; synthetic Other; posiciones sin legs | mixto; algunos supuestos permanecen |
| Oracle subsystem | Cantina/Certora, julio 2026 | 14 Cantina, 2 bajas Certora | Swap de reporter, subconditions, arbitraje, arity/type mal configurado | 1 baja fixed y otras acknowledged; revisar despliegue |
| v1.2 | Quantstamp/Cantina, agosto 2026 | QSP 11 sin alta/media; Cantina 12 sin alta/media | Other fraccional, Chainlink duration=0, eventos batch vacíos, bridge fuera de orden | varias fixed; varias acknowledged |
| Deposit Wallet | Cantina/Certora/Zellic, marzo-junio 2026 | Varias medias/bajas | Beacon drift, returnbomb ERC-1271, ghost fills, session auth, ownership, batch griefing | medias generalmente fixed; supuestos accepted |
| CTF Exchange V2 | Cantina, marzo 2026 | 31: 0 crítica/alta, 5 medias, 6 bajas, 20 info | Fill cero, tokenId 0, fee cap, reconciliación complementaria, par de condición | 5 medias y 6 bajas fixed; info mixtas |
| CTF Exchange V2 | Quantstamp, marzo 2026 | 5: 1 media, 3 bajas, 1 info | Overcharge/underfill, builder attribution, cashValue=0, rutas, overflow | todos fixed según informe |
| V1 contracts | 7 informes 2020-2024 | hallazgos históricos | Críticas/altas históricas de Exchange/NegRisk/UMA fueron corregidas | solo reconstrucción legacy |

## Riesgos vigentes para la capa de datos

1. **Eventos semánticamente engañosos.** Informes detectaron volúmenes builder sobredeclarados, parámetros de evento erróneos, bits sucios y `TransferBatch` vacío. Guardar raw topics/data y transaction receipt; no aceptar el evento decodificado como verdad final sin invariantes.
2. **CLOB vs settlement.** Una orden puede expirar/cancelarse offchain sin evento onchain. El estado “open/cancelled/expired” requiere API/WS, mientras que fills y movimientos económicos se confirman onchain.
3. **Transferencias externas.** Los tokens pueden llegar sin compra. Un PnL basado solo en fills inventa costo cero o ignora inventario; se requiere lot accounting explícito y procedencia.
4. **Reorgs Polygon.** Usar identidad `(chain,address,tx_hash,log_index)`, block hash, estado canónico y rollback. `rindexer` usa una distancia conservadora específica de Polygon de 200 bloques; es un punto de partida, no una garantía de finalidad.
5. **Proxies/upgrades.** Registrar implementation slot y bytecode hash por bloque. Un ABI actual aplicado hacia atrás puede decodificar mal la historia.
6. **Redondeo y unidades.** pUSD/USDC y outcome tokens usan unidades enteras; toda métrica debe conservar raw integer y decimals. Nunca calcular PnL con `float`.
7. **NegRisk.** Splits, conversions y redemptions no son trades y pueden crear/destruir varios legs. La identidad `marketId/questionId/index/outcome` es obligatoria.
8. **Órdenes stale.** En V2 el timestamp no es expiration firmada. La estrategia debe imponer TTL local, cancel-all al perder sincronía y reconciliación de open orders.
9. **Wallets inteligentes.** EOA, proxy, Safe y ERC-1271 no prueban una misma persona. El campo maker es fuente de fondos; signer solo es autoridad. No agrupar identidades sin evidencia adicional.
10. **Privilegios.** Operator/admin/reporter/beacon son riesgos del sistema y señales operativas. Monitorizar cambios de rol, upgrades, pausas y configuración de fees.

## Controles P0

- Verificar bytecode/proxy implementation desplegado contra commit auditado antes de autorizar capital.
- Pin de SDK V2 y test vector EIP-712 para ambos verifying contracts.
- Raw log lake reorg-safe + ledger determinista reconstruible.
- Circuit breaker ante gap WS, RPC disagreement, reorg, cambio de implementación, address-book drift o saldo inexplicable.
- Llave con alcance mínimo: session key/KMS donde aplique; withdrawals fuera del proceso de trading.
- Allowlist de contratos y métodos; límites diarios, por mercado y por orden.

## Restricciones de licencia

`ctf-exchange-v2` está bajo BUSL-1.1 y declara cambio a MIT el 2030-03-27. Antes de esa fecha, su código no debe incorporarse a un sistema de producción fuera de los usos permitidos sin revisión legal. ABI, eventos y comportamiento públicamente observable pueden documentarse; copiar implementación es otra decisión. `polymarket-subgraph` es LGPL-3.0. En `ethereum-multicall` y `ethereum-erc20-token-balances-multicall` el metadata del paquete declara ISC mientras el archivo LICENSE declara MIT: tratar como conflicto hasta revisión.

## Límite de esta auditoría

Es investigación técnica, no certificación, asesoría legal ni garantía de ausencia de vulnerabilidades. Los estados “fixed/resolved/acknowledged” son los declarados por cada informe; deben contrastarse con el bytecode actual.
"""

write_text("polymarket_security_findings.md", security_findings)


quick_wins = """
# Quick wins para ProyectoBotV4

## P0 — antes de usar capital

1. Crear `contract_registry` con chain, address, proxy/implementation, ABI hash, valid_from/to block y fuente.
2. Separar decodificadores `clob_v1`, `clob_v2_ctf` y `combo_v2`; rechazar eventos de ABI desconocida.
3. Guardar raw logs inmutables con block hash y canonical status; cursor y lote en una transacción.
4. Expandir `TransferBatch` y conservar el índice interno del elemento para trazabilidad.
5. Añadir reconciliación triple: CLOB orders/fills, ledger derivado y balances onchain.
6. Cambiar cualquier `float` monetario por enteros atómicos/Decimal.
7. Implementar TTL local y cancel-all al detectar feed stale, gap, reconexión no resuelta o cambio de address book.
8. Crear vectores de firma V2 para CTF y NegRisk; comprobar domain exchange v2 frente a auth v1.
9. Clasificar `split/merge/convert/redeem/wrap/unwrap/transfer` como flujos de inventario, nunca como fills.
10. Exigir verificación de bytecode/proxy implementation antes de habilitar ejecución.

## P1 — siguiente iteración

1. Prototipo de rindexer sidecar → Postgres/DuckDB, comparado contra el colector actual.
2. Reorg simulator y pruebas de crash entre insert y avance de cursor.
3. Ledger de lotes con costo, procedencia, realized/unrealized y transferencias entre wallets.
4. Monitor de admin/operator/beacon/pauses/fee changes.
5. Multicall para balances, approvals y `balanceOfBatch`.
6. Captura simultánea REST+WS+onchain con medidores de latencia y secuencia.
7. Panel de data quality: duplicados, huérfanos, gaps, decode failures y drift.

## P2 — después de estabilidad

1. Mover serving concurrente a Postgres solo si DuckDB deja de cubrir el volumen.
2. Cola dedicada de acciones onchain con patrones rrelayer y signer KMS/HSM.
3. Clasificador de wallets por comportamiento, manteniendo hipótesis de identidad separadas.
4. Event-time backtests que modelen latencia, fill parcial, queue position, fees, gas y reorg.
5. Indexación Combos cuando el ABI verificado y vectores de token ID estén disponibles.
"""

write_text("polymarket_quick_wins.md", quick_wins)


architecture = """
# Recomendaciones de arquitectura

## Resultado

Mantener el bot de estrategia separado de la adquisición y del ledger. La prioridad no es un bot que “ve más rápido”, sino un sistema que pueda explicar exactamente por qué cree que una posición, fill o PnL existe.

## Capas propuestas

| Capa | Responsabilidad | Persistencia/contrato |
|---|---|---|
| Source adapters | Gamma/Data API, CLOB REST/WS, Polygon RPC | Payload raw + received_at + source sequence |
| Chain indexer | Logs, receipts, blocks, proxy state y reorgs | `raw_chain_logs`, `blocks`, `canonicality`, cursor atómico |
| Decoders | ABI por address y rango de bloques | Output versionado; nunca sobrescribir raw |
| Normalizer | Convierte V1/V2/CTF/NegRisk/Combo a hechos económicos | `fills`, `transfers`, `inventory_actions`, `resolutions`, `cashflows` |
| Ledger | Lots, costo, realized/unrealized, fees y reconciliación | DuckDB primero; cálculo determinista e idempotente |
| Strategy | Señales y sizing sin llaves | Lee snapshots consistentes; no escribe ledger |
| Risk/execution | Límites, TTL, cancelación, firmas y órdenes | Estado de orden con máquina explícita |
| Onchain executor | approvals/wrap/redeem | Cola separada, allowlist, KMS/session key |
| Observability | Calidad, latencia, gaps, reorgs, upgrades | Métricas, alertas y kill switch |

## Esquema mínimo del ledger

- `raw_chain_logs(chain_id,address,block_number,block_hash,tx_hash,tx_index,log_index,topics,data,canonical,observed_at)`.
- `source_messages(source,channel,sequence,received_at,event_time,payload_hash,payload)`.
- `contract_versions(address,implementation,abi_hash,valid_from_block,valid_to_block,source)`.
- `fills(fill_id,order_hash,maker,taker,side,token_id,base_units,quote_units,fee,builder,tx_hash,log_index)`.
- `inventory_actions(action_id,type,wallet,token_id,quantity,collateral,condition_id,market_id,provenance)`.
- `lots(wallet,token_id,lot_id,opened_by,quantity,cost_quote,closed_quantity)`.
- `order_states(order_id,state,source_time,received_at,reason,version)`.
- `reconciliations(wallet,block_number,ledger_balance,onchain_balance,delta,status)`.

## Reglas invariantes

1. Cada registro derivado enlaza al payload/log raw.
2. Un replay desde raw produce el mismo hash de ledger.
3. Ningún cursor avanza sin commit del lote.
4. Un reorg invalida derivados por dependencia, no borra evidencia.
5. `TransferBatch` genera N movimientos y conserva el parent log.
6. Suma de cantidades y flujos de colateral concilia por transacción según match type.
7. La ausencia de un evento de cancelación no convierte una orden en abierta: manda la máquina de estados multi-fuente.
8. La identidad de wallet es una dimensión probabilística separada de la contabilidad.

## Copy trading

Copiar una wallet exige replicar la **exposición económica**, no repetir ciegamente un fill observado. Esperar confirmación onchain introduce latencia; seguir solo CLOB puede incluir fills reorganizados o estados transitorios. El sistema debe comparar precio actual vs precio del líder, liquidez, slippage, exposición ya existente y resolución. Si el edge residual después de latencia/fees es negativo, no copiar.

## Market making y BTC 5m

- Mantener estado local del libro con secuencia y resync; ante gap, retirar cotizaciones.
- Modelar queue position y fill parcial; un backtest con mid-price no es válido.
- Imponer inventory skew, max adverse selection, latencia por fuente y maker/taker economics reales.
- Para BTC 5m, la resolución/oracle y el timestamp exacto son parte del instrumento. Guardar fuente, ventana y regla vigentes; no suponer que el precio spot propio es idéntico al de resolución.

## Decisión sobre repositorios

- `rindexer`: mejor candidato externo. Primero benchmark sidecar y auditoría de su carrera reorg/commit documentada.
- `rrelayer`: tomar controles y, si se adopta, limitarlo a transacciones onchain. No es el motor CLOB.
- `polymarket-subgraph`: usar como catálogo conceptual V1, no como ledger ni decodificador V2.
- `ctf-exchange-v2`: ABI/especificación esenciales; código BUSL no se incorpora sin revisión de licencia.
- utilidades TypeScript antiguas: traducir patrones al stack Python; evitar sumar runtimes por conveniencia.
"""

write_text("polymarket_architecture_recommendations.md", architecture)


repository_lifecycle = [
    {"repository": "rindexer", "branches": 149, "tags": 81, "latest_release": "v0.43.1", "commits": 882, "open_issues": 31, "open_pull_requests": 4, "workflows_seen": 7, "evidence": "GitHub page/API + shallow local clone"},
    {"repository": "rrelayer", "branches": 60, "tags": 34, "latest_release": "v0.14.0", "commits": 422, "open_issues": 12, "open_pull_requests": 5, "workflows_seen": 3, "evidence": "GitHub page/API + shallow local clone"},
    {"repository": "ctf-exchange-v2", "branches": 1, "tags": 0, "latest_release": None, "commits": 78, "open_issues": 2, "open_pull_requests": 2, "workflows_seen": 2, "evidence": "GitHub page + shallow local clone"},
    {"repository": "contract-security", "branches": 1, "tags": 0, "latest_release": None, "commits": 16, "open_issues": 0, "open_pull_requests": 0, "workflows_seen": 0, "evidence": "GitHub page + full small clone"},
    {"repository": "polymarket-subgraph", "branches": 1, "tags": 1, "latest_release": None, "commits": 398, "open_issues": 10, "open_pull_requests": 7, "workflows_seen": 0, "evidence": "GitHub page + shallow local clone"},
    {"repository": "ethereum-multicall", "branches": 3, "tags": 32, "latest_release": "latest tag 2.26.0", "commits": None, "open_issues": 18, "open_pull_requests": None, "workflows_seen": 0, "evidence": "GitHub API + shallow local clone"},
    {"repository": "reth-indexer", "branches": 2, "tags": 0, "latest_release": None, "commits": None, "open_issues": 15, "open_pull_requests": None, "workflows_seen": 0, "evidence": "GitHub API + shallow local clone"},
]


kb = {
    "schema_version": "1.0.0",
    "as_of": AS_OF,
    "scope": {
        "profile": "https://github.com/joshstevens19?tab=repositories",
        "profile_repository_count": len(inventory),
        "contract_security_repository": "https://github.com/Polymarket/contract-security",
        "contract_security_pdf_count": 35,
        "contract_security_pages": 915,
        "additional_ctf_exchange_audits": 2,
    },
    "canonical_sources": [
        "https://docs.polymarket.com/resources/contracts",
        "https://docs.polymarket.com/v2-migration",
        "https://docs.polymarket.com/concepts/positions-tokens",
        "https://docs.polymarket.com/trading/overview",
        "https://docs.polymarket.com/trading/session-keys",
        "https://github.com/Polymarket/contract-security",
        "https://github.com/Polymarket/ctf-exchange-v2",
    ],
    "core_conclusions": [
        "CLOB V2 y Combos/Polymarket V2 son grafos de contratos diferentes y requieren decodificadores separados.",
        "CLOB V2 está activo desde 2026-04-28 y las órdenes V1 ya no funcionan en producción.",
        "Un ledger forense requiere CLOB REST/WS + logs onchain + balances + metadata; ningún feed aislado basta.",
        "Los repositorios polymarket-v2 y deposit-wallet no fueron públicamente clonables al corte; sus auditorías sí son públicas.",
        "rindexer aporta los patrones más valiosos, pero su carrera reorg/commit documentada debe mitigarse.",
        "ctf-exchange-v2 es BUSL-1.1 hasta 2030-03-27; no copiar código de producción sin revisión legal.",
    ],
    "contracts": contract_rows,
    "events": event_rows,
    "repository_ranking": ranking,
    "repository_lifecycle": repository_lifecycle,
    "reusable_components": [dict(zip(["repository", "component", "reuse_type", "tier", "license", "decision", "notes"], row)) for row in reusable],
    "dependencies": [dict(zip(["component", "dependency", "purpose", "stage", "control"], row)) for row in dependencies],
    "data_quality_rules": [
        "dedupe chain logs by chain/address/tx_hash/log_index",
        "persist block_hash and canonical status",
        "commit event batch and cursor atomically",
        "expand ERC1155 batch events deterministically",
        "version ABI by address and block interval",
        "retain integers at native precision",
        "reconcile CLOB, ledger and onchain balances",
        "never infer wallet identity from signer/maker alone",
    ],
    "limitations": [
        "GitHub unauthenticated API budget became nearly exhausted during collection.",
        "Private/unavailable source code prevented exact Combo event ABI extraction.",
        "Audit statuses were not independently proven against deployed bytecode.",
        "This is technical research, not a security guarantee or legal opinion.",
    ],
}

(ROOT / "polymarket_github_knowledge_base.json").write_text(
    json.dumps(kb, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)


report = f"""
# Forensia total de GitHub para el proyecto principal Polymarket

**Fecha de corte:** {AS_OF}  
**Objetivo:** convertir repositorios, contratos y auditorías en decisiones implementables para indexer, ledger, wallet analytics, copy trading, market making y mercados BTC 5m.

## 1. Respuesta ejecutiva

El mayor valor del perfil de Josh Stevens no está en una estrategia de trading publicada. Está en tres patrones de infraestructura: **indexación reorg-safe (`rindexer`)**, **operación segura de transacciones (`rrelayer`)** y **lecturas EVM masivas (`ethereum-multicall`/blooms)**. La pieza contractual indispensable está fuera de esos 49 repositorios: `Polymarket/ctf-exchange-v2`, más los informes de `Polymarket/contract-security`.

La recomendación principal es construir primero un **PolyLedger Sentinel**: una capa de evidencia que preserve logs crudos, reconstruya inventario y PnL, detecte reorgs/upgrades y concilie API/CLOB/cadena. Copiar wallets o automatizar market making antes de esa capa produce cifras plausibles pero no auditables.

## 2. Qué se analizó

- 49 repositorios públicos visibles en `joshstevens19`.
- Metadatos: descripción, actividad, lenguaje, forks, ramas/tags disponibles, licencia declarada y relevancia.
- Clones locales profundos de 12 repositorios clave/relacionados.
- 35 auditorías del repositorio `contract-security` (915 páginas) y 2 auditorías adicionales de CTF Exchange V2.
- Código y tests de CTF Exchange V2, rindexer, rrelayer y polymarket-subgraph.
- Documentación oficial actual de contratos, migración V2, tokens/positions, trading y session keys.

## 3. Ranking de los 49 repositorios

| # | Repo | Tier | Score | Decisión |
|---:|---|:---:|---:|---|
""" + "\n".join(
    f"| {r['rank']} | [{r['repository_name']}]({r['url']}) | {r['tier']} | {r['score_0_100']} | {r['recommended_action']} |"
    for r in ranking
) + """

### Lectura del ranking

- **S:** adoptar patrón o evaluar integración ahora.
- **A:** prototipo acotado; valor real con restricciones.
- **B:** referencia, no dependencia automática.
- **C:** periférico o viejo.
- **D:** sin valor para el roadmap actual.

La clasificación mide utilidad para este proyecto, no prestigio ni calidad general del upstream.

### Estado de ramas, tags, releases, commits, issues, PRs y workflows

El inventario CSV conserva estos campos para los 49 repositorios. La inspección profunda priorizó Tier S/A y repositorios Polymarket relacionados:

| Repo | Señales al corte | Lectura |
|---|---|---|
| `rindexer` | 149 ramas, 81 tags según API; release visible `v0.43.1`; 882 commits; 31 issues y 4 PRs visibles; 7 workflows locales | Desarrollo muy activo y release automation madura; también declara que es nuevo, cambiante y con bugs posibles. |
| `rrelayer` | 60 ramas, 34 tags; release visible `v0.14.0`; 422 commits; 12 issues y 5 PRs; workflows CI/Docker/Pages | Activo, pero el backlog abierto incluye Python SDK, EIP-7702, integración con rindexer y edge cases KMS/schema: integrar solo tras prueba. |
| `ctf-exchange-v2` | rama `main`, sin tags/releases en clone; 78 commits; 2 issues/2 PRs; workflows test y review | Fuente contractual actual, con auditorías versionadas; fijar siempre commit/bytecode, nunca “main” flotante. |
| `contract-security` | rama `main`, 0 tags; 16 commits; 0 issues/0 PRs; sin workflow | Repositorio documental de despliegues/auditorías, no biblioteca de código. |
| `polymarket-subgraph` | rama `main`, 1 tag capturado; 398 commits; 10 issues/7 PRs; sin workflow local | Aún público y útil como historia/esquema, pero los ABIs/referencias son CLOB V1. |
| `ethereum-multicall` | 3 ramas, 32 tags, 18 open issues API; último push 2025-12 | Más maduro que otras utilidades, pero ethers v5 y conflicto MIT/ISC aconsejan usar el patrón, no acoplar el bot. |
| `reth-indexer` | 2 ramas, 0 tags, 15 open issues API; último push 2024-01 | I+D sin releases; no usar como base operativa. |

Los contadores de GitHub son una fotografía y `open_issues_count` de la API agrega issues y PRs; por eso, cuando existe página visible, el informe separa ambos. Los clones fueron de profundidad limitada: los conteos globales provienen de la API/página y los hashes/workflows de la revisión local.

## 4. Repositorios decisivos

### rindexer

Es el repositorio más transferible. Ofrece configuración por YAML, Postgres/CSV, GraphQL, factory discovery, ERC-1155 `TransferBatch`, streams y múltiples proveedores de datos. Su código actual contiene rollback por reorg, barrera para escritores y una operación de inserción masiva con cursor en la misma transacción. Para Polygon define una distancia conservadora de 200 bloques. También documenta una carrera residual entre rollback y commits en vuelo: por ello se recomienda un raw log lake inmutable y canonicalidad explícita, no copiar la implementación sin prueba adversarial.

### rrelayer

Aporta colas, gestión de nonce, historial, webhooks, rate limits, bump de gas, top-ups y backends de firma KMS/Turnkey/Fireblocks/Privy/PKCS11. Es adecuado para approvals, wrap/unwrap o redemption. No reemplaza al CLOB: colocar/cancelar órdenes se hace por API fuera de cadena y la liquidación la ejecuta el operador.

### ethereum-multicall y bloom-filters

Multicall reduce rondas RPC en reconciliación. Blooms ayudan al escanear bloques completos, con falsos positivos pero sin falsos negativos; no agregan mucho cuando el proveedor ya ejecuta `eth_getLogs` filtrado. La implementación TypeScript es secundaria: el patrón debe llevarse a la librería mantenida del stack Python.

### reth-indexer

Promete índices muy rápidos leyendo directamente la DB de reth, COPY a Postgres y Parquet/BigQuery. El propio README advierte que es I+D, con features faltantes y bugs; además exige proximidad al nodo. Es una ruta de escalado, no una dependencia P0.

### polymarket-subgraph

Enseña un esquema event-sourced y costo promedio ponderado, además de relaciones CTF/NegRisk. Pero decodifica `OrderFilled` V1, usa direcciones antiguas y no captura de forma autoritativa inventario adquirido por transferencias externas. Sirve para aprender, no para copiar el ledger ni calcular acierto/PNL de wallets actuales.

### ctf-exchange-v2

Especifica el `Order` actual, firmas, match types y eventos. El operador casa órdenes firmadas; los tres match types son complementary, mint y merge. La auditoría reveló que fills cero, tokenId cero, fee caps y rutas no complementarias necesitaban controles, reportados luego como corregidos. Su licencia BUSL restringe reutilización productiva de código hasta el cambio previsto a MIT.

## 5. CLOB V2 y contratos

Desde 2026-04-28, producción acepta CLOB V2. Cambian SDK, collateral, dominios, struct y fee model. La URL del CLOB permanece `https://clob.polymarket.com`. Para Python debe usarse `py-clob-client-v2`; el paquete V1 ya no funciona en producción.

El struct firmado elimina nonce, feeRateBps, taker y expiration, e incorpora timestamp en milisegundos, metadata y builder. `expiration` sigue en el cuerpo de `POST /order`, pero no está firmado. Esto crea una obligación operacional: TTL local, cancelación y reconciliación de órdenes abiertas no pueden derivarse solo de la cadena.

Los CTF outcome tokens siguen siendo ERC-1155. Trades, transferencias, splits, merges y redemptions son causas distintas de cambios de saldo. Si se mezclan, la tasa de acierto y el PnL de una cartera serán falsos.

## 6. Smart contracts y auditorías

La familia Combos auditada contiene PositionManager, Exchange, Router, Binary/NegRisk/Combinatorial modules, OracleAggregator y reporters. Tiene controles complejos de migración, resolución y bridge. Los hallazgos más graves fueron migración no canónica con pUSD sin respaldo, alias de posiciones combinatorias, overredeem y condiciones/resoluciones que podían bloquear activos. Los informes posteriores muestran correcciones y verificación formal parcial, pero también riesgos aceptados de configuración, oráculos, pausas y orden de mensajes cross-chain.

La lección práctica no es evitar Polymarket; es versionar contratos/ABIs, verificar bytecode desplegado y tratar cada familia como un protocolo separado. La página oficial de contratos es la fuente de verdad y ya difiere de direcciones de adaptadores conservadas en README históricos.

## 7. Diseño del indexer

El indexer debe ser at-least-once en adquisición y exactly-once lógico en materialización. Cada log se identifica por chain/address/tx/log_index, conserva block hash y cambia de canónico a huérfano sin borrarse. Cursor y lote se confirman juntos. Los derivados se regeneran desde raw.

Se recomiendan dos tiempos: `event_time` y `received_at`. Para backtests de copia/MM importa lo que el bot podía saber en ese momento, no solo el timestamp de bloque. REST, WS y RPC deben tener sus propios cursores, health y gap detection.

## 8. Ledger y wallet analytics

Una cartera puede comprar, recibir, dividir, combinar, convertir, redimir o transferir tokens. Maker y signer pueden ser distintos. Proxy, Safe, EOA y Deposit Wallet no demuestran identidad común. Por tanto:

- PnL debe basarse en lotes y procedencia, no solo promedio de fills.
- Win rate solo se calcula sobre riesgo económico cerrado y con denominador explícito.
- Transferencias entre wallets conocidas deben conservar basis sin inventar ganancia.
- Redemptions y posiciones abiertas se separan de trades cerrados.
- “Otra cartera probable” es una hipótesis con evidencia/score, nunca un hecho por una transferencia aislada.

## 9. Bots, copy trading y MM

Para copy trading, el fill del líder es una señal retrasada. La orden seguidora solo se envía si el precio actual conserva edge tras slippage, fees, latencia y exposición. Se imponen límites por market/event/wallet y un kill switch por drift de datos.

Para market making, backtests deben modelar libro, prioridad de cola, fill parcial, adverse selection, reconexiones y cambios de fee. Un test que compra al mid histórico “parece real” pero no lo es. Para BTC 5m, además se versiona la regla exacta de resolución y el feed/ventana; precio spot propio y precio de resolución no se presuponen iguales.

## 10. Seguridad operacional

No almacenar una private key desnuda junto al proceso de estrategia. Preferir alcance mínimo y separación: la estrategia produce intenciones, risk las valida y un signer/session key firma. Las session keys oficiales están en beta, se limitan a Deposit Wallet, tienen vigencia máxima documentada y no permiten retirar; requieren builder approval. Para acciones onchain, usar allowlists de contrato/método y límites de valor.

## 11. Qué copiar y qué no

**Copiar/reconstruir:** cursor atómico, reorg handling, writer barrier, factory discovery, expansión ERC-1155, reconciliación por multicall, colas/allowlists/KMS y ABIs versionadas.

**No copiar:** subgraph V1 como PnL actual; contratos BUSL a producción; direcciones de README sin validación; SDK V1; cálculo monetario con float; heurística de identidad basada en signer; benchmark propio de un README como SLA.

## 12. Limitaciones y nivel de confianza

Alta confianza en direcciones/orden/eventos CTF V2 porque convergen documentación oficial y fuente pública. Alta confianza en el inventario de 49 repositorios. Media-alta en estados de hallazgos, pues proceden de PDFs de auditoría pero falta probar bytecode desplegado. Media en arquitectura Combo detallada porque el código fuente auditado no estaba públicamente accesible. Baja/nula para cualquier afirmación de rentabilidad: GitHub y auditorías no prueban edge financiero.

## 13. Archivos producidos

- `github_repository_inventory.csv`: inventario bruto de 49 repositorios.
- `github_repository_ranking.csv`: ranking total S–D.
- `polymarket_contract_map.md`: contratos, direcciones y separación CTF/Combo.
- `polymarket_event_catalog.csv`: eventos y semántica contable.
- `polymarket_security_findings.md`: consolidado de auditorías/riesgos.
- `polymarket_reusable_components.csv`: componentes y decisión de reutilización.
- `polymarket_dependency_map.csv`: mapa de dependencias objetivo.
- `polymarket_github_knowledge_base.json`: base estructurada para futuras tareas.
- `polymarket_quick_wins.md`: backlog P0/P1/P2.
- `polymarket_architecture_recommendations.md`: arquitectura objetivo.
- `github_research_handoff.md`: este informe y traspaso.

# ==========================================================
# TRASPASO — GITHUB INTELLIGENCE FOR POLYMARKET PROJECT
# ==========================================================

SOURCES ANALYZED: Perfil joshstevens19; 49 repos públicos; contract-security; 35 PDFs/915 páginas; 2 auditorías CTF Exchange V2; documentación oficial actual; 12 clones locales.

REPOSITORIES ANALYZED: Los 49 del inventario; profundidad máxima en rindexer, rrelayer, reth-indexer, ethereum-multicall, ethereum-bloom-filters, ethereum-abi-types-generator, ethereum-erc20-token-balances-multicall, evmc, evm-cli, ctf-exchange-v2, polymarket-subgraph y contract-security.

MOST RELEVANT REPOSITORIES: rindexer; rrelayer; ethereum-multicall; ethereum-bloom-filters; ctf-exchange-v2; polymarket-subgraph (legacy/reference); contract-security.

CRITICAL FINDINGS: CLOB V2 != Combos V2; V1 orders/SDK no longer production; ledger multi-fuente obligatorio; private Combo/wallet source unavailable; audit fixed != deployed bytecode verified.

SMART CONTRACT FINDINGS: Direcciones actuales versionadas; proxies/upgrades deben monitorearse; migración/bridge/oracles fueron la principal superficie de riesgo.

CLOB FINDINGS: EIP-712 domain v2; API auth v1; timestamp ms; no nonce/feeRateBps/taker/expiration firmados; expiration sigue en wire; operator fees en match.

CONDITIONAL TOKENS FINDINGS: ERC-1155; split/merge/redeem/transfer son inventario, no trades; IDs deben obtenerse con lógica oficial y vectores.

NEGRISK FINDINGS: market/question/index/outcome son claves; conversiones y synthetic Other exigen reglas propias; V1 adapter está deprecated.

INDEXER FINDINGS: raw logs inmutables; canonical flag; cursor+lote atómicos; rollback reorg; gap detection; ABI por block range; expandir TransferBatch.

WALLET ANALYTICS FINDINGS: maker != signer; wallet type != identidad; lot accounting y procedencia; transferencias externas rompen PnL basado solo en fills.

BOT FINDINGS: CLOB execution fuera de cadena; rrelayer solo para ops onchain; TTL local; kill switch; backtest event-time con queue/fills/slippage.

SECURITY FINDINGS: múltiples auditorías y formal verification; riesgos aceptados persisten; bytecode/proxy verification y privilege monitoring son P0.

REUSABLE COMPONENTS: reorg coordinator; atomic cursor; writer barrier; ERC-1155 batch; factory discovery; multicall; bloom prefilter; relayer queue/KMS/allowlist; ABI type generation.

LIBRARIES: evaluar py-clob-client-v2, rindexer sidecar, proveedor Polygon redundante, Decimal/integers, DuckDB ahora y Postgres si escala.

LICENSE RESTRICTIONS: ctf-exchange-v2 BUSL-1.1 hasta 2030-03-27; polymarket-subgraph LGPL-3.0; inconsistencias MIT/ISC en dos paquetes multicall.

DEPRECATED COMPONENTS: CLOB V1 SDK/orders; NegRisk adapter V1; polymarket-subgraph para producción V2; agent/examples históricos; repos dormantes del perfil.

TOP 20 LEARNINGS:
1) separar CLOB CTF y Combos; 2) V2 live 2026-04-28; 3) address book oficial manda; 4) ABI versionada; 5) raw antes de derived; 6) cursor atómico; 7) block hash/canonicalidad; 8) TransferBatch N movimientos; 9) splits no son compras; 10) redemptions no son ventas; 11) maker no es signer; 12) wallet no es persona; 13) expiration no firmada; 14) auth y exchange tienen versiones distintas; 15) fees llegan en match; 16) rindexer es mejor referencia; 17) reth-indexer es I+D; 18) subgraph es V1; 19) BUSL restringe copia; 20) auditoría no prueba rentabilidad.

TOP 10 IMPLEMENTATIONS:
1) contract registry; 2) raw log lake; 3) decoders por era; 4) ledger de lotes; 5) reconciliación triple; 6) EIP-712 vectors; 7) WS gap/resync; 8) reorg simulator; 9) risk/kill switch; 10) signer aislado.

ARCHITECTURE CHANGES RECOMMENDED: separar acquisition/indexer/decoder/normalizer/ledger/strategy/risk/execution/signer/observability; conservar DuckDB para análisis y evaluar rindexer sidecar.

FILES CREATED: github_repository_inventory.csv; github_repository_ranking.csv; polymarket_contract_map.md; polymarket_event_catalog.csv; polymarket_security_findings.md; polymarket_reusable_components.csv; polymarket_dependency_map.csv; polymarket_github_knowledge_base.json; polymarket_quick_wins.md; polymarket_architecture_recommendations.md; github_research_handoff.md.

CODE CREATED: tools/collect_github_forensics.ps1; tools/extract_contract_security_pdfs.py; tools/inspect_repositories.ps1; tools/build_github_forensics_artifacts.py.

P0 TASKS: verificar bytecode/proxies; contract registry; raw reorg-safe; decoders V1/V2/Combo; EIP-712 tests; reconciliación; kill switch; secretos fuera del bot.

P1 TASKS: rindexer benchmark; ledger de lotes; multicall; monitor de roles/upgrades; data quality dashboard; capture REST+WS+chain.

P2 TASKS: Postgres/streams si escala; rrelayer onchain; identity hypotheses; Combo ABI; backtest realista de copia/MM/BTC5m.

NEXT EXPERIMENT: Ejecutar un replay de 7 días para una wallet conocida con dos pipelines independientes (colector actual y rindexer), inyectar reorg/crash/gaps, y exigir balances/PnL idénticos antes de automatizar una sola orden.
"""

write_text("github_research_handoff.md", report)


print(json.dumps({
    "inventory": len(inventory),
    "ranking": len(ranking),
    "events": len(event_rows),
    "contracts": len(contract_rows),
    "reusable": len(reusable),
    "dependencies": len(dependencies),
    "files": [
        "github_repository_inventory.csv",
        "github_repository_ranking.csv",
        "polymarket_contract_map.md",
        "polymarket_event_catalog.csv",
        "polymarket_security_findings.md",
        "polymarket_reusable_components.csv",
        "polymarket_dependency_map.csv",
        "polymarket_github_knowledge_base.json",
        "polymarket_quick_wins.md",
        "polymarket_architecture_recommendations.md",
        "github_research_handoff.md",
    ],
}, ensure_ascii=False, indent=2))
