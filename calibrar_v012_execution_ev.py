from __future__ import annotations

import argparse, hashlib, json, math, sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

EXEC_DB = DATA / "execution_forward_v012.db"
SHADOW_DB = DATA / "shadow_forward_twap_transfer_v094a.db"
PREREG = DATA / "prereg_v012_execution_ev.json"
IMPL = DATA / "prereg_v012_implementation.json"
AUDIT50 = DATA / "auditoria_tecnica_v012_50.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"

EPS = 1e-9
MAX_LATENESS_MS = 2000

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load(p):
    return json.loads(p.read_text(encoding="utf-8"))

def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1<<20), b""): h.update(b)
    return h.hexdigest()

def sf(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except:
        return None

def fresh(raw):
    if not raw: return (False,None)
    try: d=json.loads(raw)
    except: return (False,None)
    ok=(d.get("twap_30s_fresh") in (1,True)
        and d.get("twap_open_fresh") in (1,True)
        and sf(d.get("twap_distance_to_open_bps")) is not None)
    return ok,d

def qtile(vals,q):
    a=sorted(vals)
    if not a: raise RuntimeError("Sin datos para cuantíl")
    pos=(len(a)-1)*q
    lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi: return a[lo]
    w=pos-lo
    return a[lo]*(1-w)+a[hi]*w

def verify():
    if not PREREG.exists() or not IMPL.exists():
        raise RuntimeError("Faltan preregistros v0.12")
    p=load(PREREG); i=load(IMPL)
    if i.get("parent_prereg_sha256") != sha(PREREG):
        raise RuntimeError("El preregistro cambió después de congelar implementación")
    if i.get("calibration_uses_labels") is not False:
        raise RuntimeError("calibration_uses_labels debe ser False")
    return p,i

def ro(path):
    if not path.exists(): raise RuntimeError(f"No existe {path}")
    c=sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=20)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c

def exec_pairs():
    c=ro(EXEC_DB)
    try:
        rows=c.execute("""
        SELECT condition_id,slug,market_start_ms,horizon_seconds,lateness_ms,status,
               tick_size,min_order_size,
               up_best_bid,up_best_ask,up_executable,up_vwap_buy,up_total_cost_per_share,
               down_best_bid,down_best_ask,down_executable,down_vwap_buy,down_total_cost_per_share
        FROM execution_snapshots
        WHERE horizon_seconds IN (120,60)
        ORDER BY market_start_ms,condition_id,horizon_seconds DESC
        """).fetchall()
    finally: c.close()
    by={}
    for r in rows: by.setdefault(str(r["condition_id"]),{})[int(r["horizon_seconds"])]=r
    out=[]
    for cid,h in by.items():
        if 120 in h and 60 in h:
            out.append((cid,h[120],h[60]))
    out.sort(key=lambda x:(int(x[2]["market_start_ms"]),x[0]))
    return out

def shadow():
    c=ro(SHADOW_DB)
    try:
        a=c.execute("""SELECT condition_id,feature_json FROM shadow_diagnostics
        WHERE horizon_seconds=120 AND status='SAVED' AND feature_json IS NOT NULL""").fetchall()
        b=c.execute("""SELECT condition_id,feature_json FROM shadow_features
        WHERE horizon_seconds=60 AND feature_json IS NOT NULL""").fetchall()
    finally: c.close()
    d120={}; d60={}
    for r in a:
        ok,d=fresh(r["feature_json"])
        if ok: d120[str(r["condition_id"])]=d
    for r in b:
        ok,d=fresh(r["feature_json"])
        if ok: d60[str(r["condition_id"])]=d
    return d120,d60

def rowok(r):
    if str(r["status"])!="SAVED": return False
    if r["lateness_ms"] is None or int(r["lateness_ms"])>MAX_LATENESS_MS: return False
    if sf(r["tick_size"]) is None or sf(r["min_order_size"]) is None: return False
    if int(r["up_executable"] or 0)!=1 or int(r["down_executable"] or 0)!=1: return False
    for side in ("up","down"):
        bid=sf(r[f"{side}_best_bid"]); ask=sf(r[f"{side}_best_ask"])
        vwap=sf(r[f"{side}_vwap_buy"]); cost=sf(r[f"{side}_total_cost_per_share"])
        if None in (bid,ask,vwap,cost): return False
        if vwap+1e-12 < ask: return False
    return True

def implied(r):
    ub,ua,db,da=[sf(r[k]) for k in ("up_best_bid","up_best_ask","down_best_bid","down_best_ask")]
    if None in (ub,ua,db,da): return None
    um=(ub+ua)/2; dm=(db+da)/2
    return um/(um+dm) if um+dm>0 else None

def eligible():
    pairs=exec_pairs()
    s120,s60=shadow()
    out=[]
    for cid,h120,h60 in pairs:
        if cid not in s120 or cid not in s60: continue
        if not rowok(h120) or not rowok(h60): continue
        p120,p60=implied(h120),implied(h60)
        t120=sf(s120[cid].get("twap_distance_to_open_bps"))
        t60=sf(s60[cid].get("twap_distance_to_open_bps"))
        if None in (p120,p60,t120,t60): continue
        tm=t60-t120
        direction=1 if tm>0 else (-1 if tm<0 else 0)
        if direction==0: continue
        mm=(p60-p120)*10000.0
        resp=direction*mm/(abs(tm)+EPS)
        side_cost=sf(h60["up_total_cost_per_share"] if direction>0 else h60["down_total_cost_per_share"])
        out.append({
            "condition_id":cid,
            "market_start_ms":int(h60["market_start_ms"]),
            "twap_move_bps":tm,
            "abs_twap_move_bps":abs(tm),
            "market_response_ratio":resp,
            "direction":direction,
            "chosen_side_cost_60":side_cost,
            "late120":int(h120["lateness_ms"]),
            "late60":int(h60["lateness_ms"]),
            "up_ask":sf(h60["up_best_ask"]),
            "up_vwap":sf(h60["up_vwap_buy"]),
            "down_ask":sf(h60["down_best_ask"]),
            "down_vwap":sf(h60["down_vwap_buy"]),
        })
    out.sort(key=lambda x:(x["market_start_ms"],x["condition_id"]))
    return out

def status():
    verify()
    e=eligible()
    print("="*70)
    print("STATUS V0.12 EXECUTION-EV")
    print("="*70)
    print("ELEGIBLES:",len(e))
    print("AUDIT50 LISTA:",len(e)>=50)
    print("DEVELOPMENT100 LISTO:",len(e)>=100)
    print("FORWARD100 COMPLETO:",len(e)>=200)
    print("FALTAN PARA 50:",max(0,50-len(e)))
    print("FALTAN PARA 100:",max(0,100-len(e)))
    print("FALTAN PARA 200:",max(0,200-len(e)))
    print("NO lee labels ni PnL.")
    print("DINERO REAL: BLOQUEADO")
    print("="*70)

def audit50():
    verify()
    e=eligible()
    if len(e)<50:
        print("AUDIT50 TODAVIA NO LISTA:",len(e),"/50")
        print("FALTAN:",50-len(e))
        print("NO lee labels ni PnL.")
        return
    a=e[:50]
    viol=sum(1 for r in a if r["up_vwap"]+1e-12<r["up_ask"] or r["down_vwap"]+1e-12<r["down_ask"])
    report={
        "schema":"auditoria_tecnica_v012_50",
        "created_at":now(),
        "rows":50,
        "labels_read":False,
        "pnl_read":False,
        "max_lateness_120_ms":max(r["late120"] for r in a),
        "max_lateness_60_ms":max(r["late60"] for r in a),
        "vwap_lt_ask_violations":viol,
        "technical_pass":viol==0 and max(r["late120"] for r in a)<=2000 and max(r["late60"] for r in a)<=2000,
        "real_money":"BLOQUEADO"
    }
    if not AUDIT50.exists():
        AUDIT50.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    else:
        report=load(AUDIT50)
    print("="*78)
    print("V0.12 AUDITORIA TECNICA 50 - SIN LABELS/PNL")
    print("="*78)
    print("TECHNICAL PASS:",report["technical_pass"])
    print("MAX LATENESS 120:",report["max_lateness_120_ms"])
    print("MAX LATENESS 60 :",report["max_lateness_60_ms"])
    print("VWAP<ASK VIOLATIONS:",report["vwap_lt_ask_violations"])
    print("ARCHIVO:",AUDIT50)
    print("DINERO REAL: BLOQUEADO")

def freeze_thresholds():
    p,i=verify()
    if THRESHOLDS.exists():
        d=load(THRESHOLDS)
        print("THRESHOLDS V0.12 YA CONGELADOS")
        print("STANDARD:",d["thresholds"]["standard"])
        print("STRICT:",d["thresholds"]["strict"])
        return
    e=eligible()
    if len(e)<100:
        print("CALIBRACION TODAVIA NO LISTA:",len(e),"/100")
        print("FALTAN:",100-len(e))
        print("NO lee labels ni PnL.")
        return
    dev=e[:100]
    shocks=[r["abs_twap_move_bps"] for r in dev]
    responses=[r["market_response_ratio"] for r in dev]
    s=i["thresholds"]["standard"]; t=i["thresholds"]["strict"]
    th={
        "standard":{
            "shock_abs_bps_min":qtile(shocks,float(s["shock_abs_quantile"])),
            "market_response_ratio_max":qtile(responses,float(s["response_ratio_quantile"]))
        },
        "strict":{
            "shock_abs_bps_min":qtile(shocks,float(t["shock_abs_quantile"])),
            "market_response_ratio_max":qtile(responses,float(t["response_ratio_quantile"]))
        }
    }
    bands=p["execution_cost_bands"]
    counts={}
    for name,(sev,band) in i["candidate_rules"].items():
        lo,hi=map(float,bands[band]); z=th[sev]
        counts[name]=sum(1 for r in dev if r["abs_twap_move_bps"]>=z["shock_abs_bps_min"]
                         and r["market_response_ratio"]<=z["market_response_ratio_max"]
                         and r["chosen_side_cost_60"] is not None
                         and lo<=r["chosen_side_cost_60"]<=hi)
    out={
        "schema":"prereg_v012_thresholds",
        "created_at":now(),
        "parent_prereg_sha256":sha(PREREG),
        "implementation_sha256":sha(IMPL),
        "development_rows":100,
        "labels_read":False,
        "pnl_read":False,
        "thresholds":th,
        "cost_bands":bands,
        "candidate_rules":i["candidate_rules"],
        "candidate_event_counts_without_outcomes":counts,
        "future_rows_used":0,
        "real_money":"BLOQUEADO"
    }
    THRESHOLDS.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
    print("="*88)
    print("V0.12 THRESHOLDS CONGELADOS - SIN LABELS/PNL")
    print("="*88)
    print("STANDARD:",th["standard"])
    print("STRICT:",th["strict"])
    print("EVENTOS POR CANDIDATO:")
    for k,v in counts.items(): print(" ",k,":",v)
    print("ARCHIVO:",THRESHOLDS)
    print("DINERO REAL: BLOQUEADO")

def main():
    ap=argparse.ArgumentParser()
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status",action="store_true")
    g.add_argument("--audit50",action="store_true")
    g.add_argument("--freeze-thresholds",action="store_true")
    a=ap.parse_args()
    if a.status: status()
    elif a.audit50: audit50()
    else: freeze_thresholds()

if __name__=="__main__":
    main()
