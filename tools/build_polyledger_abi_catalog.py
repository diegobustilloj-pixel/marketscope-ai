"""Extract event interface facts from the already archived research checkout.

No implementation code is copied. Source hashes/commits accompany each ABI.
The resulting catalog is a specification reference, NOT deployment verification.
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOS = ROOT / "data/github_forensics_20260911/repos"
OUTPUT = ROOT / "configs/polyledger/abi_catalog.json"


def main():
    catalog = {"schema": 1, "deployment_verified": False, "families": {}}
    paths = {"clob_v1": "Exchange.json", "ctf": "ConditionalTokens.json",
             "negrisk": "NegRiskAdapter.json", "collateral": "ERC20.json"}
    for family, filename in paths.items():
        path = REPOS / "polymarket-subgraph/abis" / filename
        payload = json.loads(path.read_text())
        abi = payload["abi"] if isinstance(payload, dict) else payload
        events = [{"type": "event", "name": e["name"], "anonymous": e.get("anonymous", False),
                   "inputs": [{k: i[k] for k in ("name", "type", "indexed")} for i in e["inputs"]]}
                  for e in abi if e["type"] == "event"]
        catalog["families"][family] = {"abi": events, "sources": [source(path, "LGPL-3.0; event interface facts only")]}
    events, sources = [], []
    repo = REPOS / "ctf-exchange-v2"
    for path in sorted((repo / "src/exchange/interfaces").glob("*.sol")):
        code = path.read_text()
        found = re.findall(r"event\s+(\w+)\s*\((.*?)\)\s*;", code, re.S)
        if not found:
            continue
        sources.append(source(path, "BUSL-1.1; event interface facts only; no implementation reused"))
        for name, fields in found:
            inputs = []
            for field in fields.split(","):
                words = field.strip().split()
                kind = "uint8" if words[0] == "Side" else words[0]
                inputs.append({"name": words[-1], "type": kind, "indexed": "indexed" in words})
            events.append({"type": "event", "name": name, "anonymous": False, "inputs": inputs})
    catalog["families"]["clob_v2_ctf"] = {"abi": events, "sources": sources}
    catalog["families"]["combo_v2"] = {"abi": [], "status": "BLOCKED_VERIFIED_ABI_AND_TOKEN_VECTORS_REQUIRED", "sources": []}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")


def source(path, license_note):
    repository = path.relative_to(REPOS).parts[0]
    commit = subprocess.check_output(["git", "-C", str(REPOS / repository), "rev-parse", "HEAD"], text=True).strip()
    relative = path.relative_to(REPOS / repository).as_posix()
    return {"url": f"https://github.com/Polymarket/{repository}/blob/{commit}/{relative}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "license_note": license_note}


if __name__ == "__main__":
    main()
