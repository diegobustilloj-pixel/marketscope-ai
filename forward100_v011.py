import argparse, hashlib, json, math, sqlite3
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data"
DB_DEFAULT=DATA/"shadow_forward_twap_transfer_v094a.db"
PR=DATA/"prereg_v011_forward100.json"
TH=DATA/"prereg_v011_event_thresholds.json"
EV=DATA/"prereg_v011_evaluator.json"
DR=DATA/"resultado_v011_event_driven_development.json"
OUT=DATA/"resultado_v011_forward100.json"
CUTOFF=1786394100000
EPS=1e-9

def load(p): return json.loads(p.read_text(encoding="utf-8"))
def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1<<20),b""): h.update(b)
    return h.hexdigest()
def sf(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else np.nan
    except: return np.nan
def finite(x):
    try: return math.isfinite(float(x))
    except: return False
def fresh(d): return d.get("twap_30s_fresh") in (1,True) and d.get("twap_open_fresh") in (1,True)
def delta(a,b,k):
    x,y=sf(a.get(k)),sf(b.get(k))
    return x-y if finite(x) and finite(y) else np.nan
def sgn(x): return 1 if finite(x) and x>0 else (-1 if finite(x) and x<0 else 0)
def cost(ask,fee,slip):
    fill=min(0.999,max(0.001,float(ask)+float(slip)))
    return fill+float(fee)*fill*(1-fill)

def verify():
    for p in (PR,TH,EV,DR):
        if not p.exists(): raise RuntimeError(f"Falta {p}")
    f=load(PR)
    if f.get("schema")!="prereg_v011_forward100": raise RuntimeError("Schema Forward100 invalido")
    if f.get("family")!="twap_shock_market_lag" or f.get("severity")!="standard": raise RuntimeError("Regla congelada inesperada")
    if int(f.get("test_rows",-1))!=100: raise RuntimeError("test_rows debe ser 100")
    if not (f.get("no_retraining") is True and f.get("no_threshold_changes") is True and f.get("no_partial_peeking") is True):
        raise RuntimeError("Protecciones de Forward100 no estan congeladas")
    if f.get("thresholds_sha256")!=sha(TH): raise RuntimeError("Thresholds cambiaron")
    if f.get("evaluator_sha256")!=sha(EV): raise RuntimeError("Evaluador cambio")
    if f.get("development_result_sha256")!=sha(DR): raise RuntimeError("Resultado development cambio")
    cur=load(TH)["thresholds"]["twap_shock_market_lag"]["standard"]
    if f.get("frozen_thresholds")!=cur: raise RuntimeError("Thresholds embebidos no coinciden")
    return f

def read_meta(c):
    d={}
    for k,v in c.execute("SELECT key,value FROM shadow_meta"):
        try:d[k]=json.loads(v)
        except:d[k]=v
    return d

def first100(db):
    c=sqlite3.connect(db)
    meta=read_meta(c)
    fee=float(meta.get("fee_rate",0.07)); slip=float(meta.get("slippage_per_share",0.005))
    f60=c.execute("""SELECT m.condition_id,m.market_start_ms,m.label,f.feature_json
    FROM shadow_features f JOIN shadow_markets m ON m.condition_id=f.condition_id
    WHERE f.horizon_seconds=60 AND f.feature_json IS NOT NULL AND m.label_verified=1
    AND m.market_start_ms>? ORDER BY m.market_start_ms,m.condition_id""",(CUTOFF,)).fetchall()
    d120=dict(c.execute("""SELECT condition_id,feature_json FROM shadow_diagnostics
    WHERE horizon_seconds=120 AND status='SAVED' AND feature_json IS NOT NULL"""))
    c.close()
    f60=[r for r in f60 if fresh(json.loads(r[3]))]
    rem=f60[50:]
    elig=[]
    for r in rem:
        if r[0] in d120 and fresh(json.loads(d120[r[0]])):
            elig.append((r,json.loads(d120[r[0]])))
            if len(elig)==100: break
    if len(elig)<100: raise RuntimeError(f"Forward100 incompleto: {len(elig)}/100")
    return elig,fee,slip

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--db",default=str(DB_DEFAULT)); ap.add_argument("--evaluate-forward100",action="store_true"); a=ap.parse_args()
    if not a.evaluate_forward100: raise SystemExit("Use --evaluate-forward100")
    frozen=verify()
    if OUT.exists():
        r=load(OUT); print("FORWARD100 YA EVALUADO"); print("VEREDICTO:",r["verdicto"]); return
    elig,fee,slip=first100(a.db)
    t=frozen["frozen_thresholds"]; shock=float(t["shock_abs_bps_min"]); respmax=float(t["market_response_ratio_max"])
    rows=[]; trades=[]
    for (cid,ms,label,raw60),d120 in elig:
        d60=json.loads(raw60)
        tw=delta(d60,d120,"twap_distance_to_open_bps")
        p60,p120=sf(d60.get("implied_up_mid_probability")),sf(d120.get("implied_up_mid_probability"))
        mm=(p60-p120)*10000 if finite(p60) and finite(p120) else np.nan
        dr=sgn(tw)
        rr=dr*mm/(abs(tw)+EPS) if dr and finite(mm) else np.nan
        ua,da=sf(d60.get("up_best_ask")),sf(d60.get("down_best_ask"))
        event=dr!=0 and finite(tw) and abs(tw)>=shock and finite(rr) and rr<=respmax and finite(ua) and finite(da)
        rows.append((int(ms),event))
        if not event: continue
        y=1 if str(label).lower()=="up" else 0
        if dr>0: side="UP"; co=cost(ua,fee,slip); pnl=y-co
        else: side="DOWN"; co=cost(da,fee,slip); pnl=(1-y)-co
        trades.append((int(ms),side,float(co),float(pnl)))
    pn=np.array([x[3] for x in trades],float) if trades else np.array([],float)
    co=np.array([x[2] for x in trades],float) if trades else np.array([],float)
    n=len(trades); wins=int((pn>0).sum()) if n else 0
    net=float(pn.sum()) if n else 0.0; roi=float(net/co.sum()) if n and co.sum()>0 else None
    mid=rows[50][0]
    h1=float(sum(x[3] for x in trades if x[0]<mid)); h2=float(sum(x[3] for x in trades if x[0]>=mid))
    pos=pn[pn>0]; share=float(pos.max()/pos.sum()) if len(pos) and pos.sum()>0 else None
    g=frozen["gates"]; fails=[]
    if n<int(g["minimum_trades"]): fails.append("MIN_TRADES")
    if net<=0:fails.append("NET_PNL")
    if roi is None or roi<=0:fails.append("ROI")
    if h1<0:fails.append("FIRST_HALF")
    if h2<0:fails.append("SECOND_HALF")
    if share is None or share>float(g["maximum_single_positive_trade_share"]):fails.append("POS_SHARE")
    verdict="PASS_FORWARD100" if not fails else "FAIL_FORWARD100"
    out={"schema":"resultado_v011_forward100","created_at":datetime.now(timezone.utc).isoformat(),
         "rule":"twap_shock_market_lag/standard","test_rows":100,"events_traded":n,"wins":wins,
         "win_rate":wins/n if n else None,"net_pnl":net,"roi_on_cost":roi,
         "first_half_net_pnl":h1,"second_half_net_pnl":h2,
         "largest_positive_trade_share":share,"failures":fails,"verdicto":verdict,
         "future_rows_beyond_first_100_used":0,"real_money":"BLOQUEADO",
         "forward_prereg_sha256":sha(PR)}
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    print("="*92); print("V0.11 EVENT-DRIVEN - FORWARD100 INDEPENDIENTE"); print("="*92)
    print("REGLA: twap_shock_market_lag / standard"); print("FILAS TEST: 100"); print("EVENTOS / TRADES:",n)
    print("WINS:",wins); print("WIN RATE:",out["win_rate"]); print("PNL NETO:",net); print("ROI SOBRE COSTO:",roi)
    print("PNL MITAD 1:",h1); print("PNL MITAD 2:",h2); print("MAX POSITIVE TRADE SHARE:",share)
    print("VEREDICTO:",verdict)
    if fails: print("FALLOS:",",".join(fails))
    print("RESULTADO:",OUT); print("DINERO REAL: BLOQUEADO"); print("="*92)
if __name__=="__main__": main()
