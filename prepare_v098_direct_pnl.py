from __future__ import annotations
import argparse, hashlib, json, math, sqlite3
from datetime import datetime, timezone
from pathlib import Path
import joblib, numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT=Path(__file__).resolve().parent; DATA=ROOT/"data"
DB_DEFAULT=DATA/"shadow_forward_twap_transfer_v094a.db"
PREREG=DATA/"prereg_v098_direct_pnl.json"
IMPL=DATA/"prereg_v098_implementation.json"
PREPARE_JSON=DATA/"prepare_v098_direct_pnl.json"
MODEL_FILE=DATA/"modelo_v098_direct_pnl.joblib"

CUTOFF_MS=1786394100000
DEV_POST=50
DEV_EXPECTED=211
MIN_TRAIN=100
BLOCK=25
GRID=(0.00,0.01,0.02,0.03,0.04,0.05)
MIN_TRADES=20
MAX_POS_SHARE=0.35

FEATURES=[
"twap_distance_to_open_bps","twap_minus_chainlink_bps",
"chainlink_return_5s_bps","chainlink_return_15s_bps","chainlink_return_30s_bps","chainlink_return_60s_bps",
"chainlink_vol_30s_bps","chainlink_vol_60s_bps","chainlink_up_fraction_30s","chainlink_state","chainlink_state_run_length",
"binance_return_5s_bps","binance_return_15s_bps","binance_return_30s_bps","binance_return_60s_bps",
"volatility_regime_ratio","implied_up_mid_probability","up_cost","down_cost"]

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda:f.read(1<<20),b""): h.update(c)
    return h.hexdigest()
def loadj(p): return json.loads(p.read_text(encoding="utf-8"))
def sf(v):
    try:
        x=float(v); return x if math.isfinite(x) else np.nan
    except: return np.nan

def verify_prereg():
    if not PREREG.exists(): raise RuntimeError(f"Falta {PREREG}")
    d=loadj(PREREG)
    if int(d["development_rows_expected"])!=DEV_EXPECTED: raise RuntimeError("development_rows_expected != 211")
    if list(d["future_test1_post_cutoff_rows"])!=[51,150]: raise RuntimeError("TEST1 != 51-150")
    if list(d["future_test2_post_cutoff_rows"])!=[151,300]: raise RuntimeError("TEST2 != 151-300")
    if list(d["features"])!=FEATURES: raise RuntimeError("Features no coinciden")
    if tuple(float(x) for x in d["predicted_pnl_threshold_grid"])!=GRID: raise RuntimeError("Grid no coincide")
    if int(d["walk_forward"]["min_train_rows"])!=MIN_TRAIN or int(d["walk_forward"]["block_rows"])!=BLOCK:
        raise RuntimeError("Walk-forward no coincide")
    return d

def freeze_impl():
    verify_prereg()
    if IMPL.exists():
        print("IMPLEMENTACION V0.9.8 YA CONGELADA - NO SE SOBRESCRIBE")
        print("ARCHIVO:",IMPL); print("DINERO REAL: BLOQUEADO"); return
    d={
      "schema":"prereg_v098_implementation","created_at":now(),
      "parent_prereg_sha256":sha(PREREG),
      "population":"base <= cutoff + exactamente primeras 50 filas validas post-cutoff; filas 51+ no usar en prepare",
      "targets":{"up":"y_up-up_cost","down":"1-y_up-down_cost"},
      "models":{
        "direct_pnl_ridge":{"two_independent_regressors":True,"alpha":100.0,"imputer":"median+indicator+keep_empty","scaler":"standard"},
        "direct_pnl_hist_gradient_boosting":{"two_independent_regressors":True,"learning_rate":0.05,"max_iter":100,"max_depth":2,"min_samples_leaf":20,"l2_regularization":5.0,"random_state":980}},
      "walk_forward":{"type":"expanding","min_train_rows":100,"block_rows":25},
      "decision":"elegir lado con mayor PnL predicho; operar si max_predicted_pnl >= threshold",
      "threshold_selection":"threshold elegible si trades>=20, net_pnl>0, ROI>0, corr(predicted,realized)>0, PnL primera mitad>=0, PnL segunda mitad>=0, largest_positive_trade_share<=0.35; elegir max net_pnl, desempate mean_pnl y threshold mayor",
      "candidate_selection":"entre candidatos elegibles elegir max net_pnl OOF, desempate mean_pnl, luego Ridge por simplicidad",
      "real_money":"BLOQUEADO"}
    IMPL.write_text(json.dumps(d,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    print("="*72); print("IMPLEMENTACION V0.9.8 CONGELADA"); print("="*72)
    print("OBJETIVO: PNL DIRECTO UP/DOWN"); print("TEST1 51-150: RESERVADO"); print("TEST2 151-300: RESERVADO")
    print("ARCHIVO:",IMPL); print("DINERO REAL: BLOQUEADO"); print("="*72)

def verify_impl():
    verify_prereg()
    if not IMPL.exists(): raise RuntimeError("Primero ejecute --freeze-implementation")
    if loadj(IMPL)["parent_prereg_sha256"]!=sha(PREREG): raise RuntimeError("Preregistro cambió después de congelar implementación")

def open_ro(p):
    con=sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro",uri=True,timeout=30); con.row_factory=sqlite3.Row; con.execute("PRAGMA query_only=ON"); return con

def meta(con):
    out={}
    for r in con.execute("SELECT key,value FROM shadow_meta"):
        try: out[str(r["key"])]=json.loads(r["value"])
        except: out[str(r["key"])]=r["value"]
    return out

def cost(ask,fee_rate,slip):
    fill=min(0.999,max(0.001,float(ask)+float(slip))); fee=float(fee_rate)*fill*(1-fill); return fill+fee

def parse(row,fee_rate,slip):
    f=json.loads(row["feature_json"])
    if f.get("twap_30s_fresh") not in (1,True) or f.get("twap_open_fresh") not in (1,True): return None
    ua,da,pm=sf(f.get("up_best_ask")),sf(f.get("down_best_ask")),sf(f.get("implied_up_mid_probability"))
    if not all(math.isfinite(x) for x in (ua,da,pm)): return None
    uc,dc=cost(ua,fee_rate,slip),cost(da,fee_rate,slip); y=1 if str(row["label"]).lower()=="up" else 0
    vals=[]
    for k in FEATURES:
        vals.append(uc if k=="up_cost" else dc if k=="down_cost" else sf(f.get(k)))
    return {"condition_id":str(row["condition_id"]),"market_start_ms":int(row["market_start_ms"]),"y":y,"x":vals,
            "up_cost":uc,"down_cost":dc,"up_realized_pnl":y-uc,"down_realized_pnl":1-y-dc}

def load_rows(db):
    con=open_ro(db)
    try:
        m=meta(con); fee=float(m.get("fee_rate",0.07)); slip=float(m.get("slippage_per_share",0.005))
        q=con.execute("""SELECT m.condition_id,m.market_start_ms,m.label,f.feature_json
        FROM shadow_features f JOIN shadow_markets m ON m.condition_id=f.condition_id
        WHERE f.horizon_seconds=60 AND f.feature_json IS NOT NULL AND m.label_verified=1
        ORDER BY m.market_start_ms,m.condition_id""").fetchall()
    finally: con.close()
    rows=[]
    for r in q:
        x=parse(r,fee,slip)
        if x is not None: rows.append(x)
    return rows,{"fee_rate":fee,"slippage_per_share":slip}

def dev_split(rows):
    base=[r for r in rows if r["market_start_ms"]<=CUTOFF_MS]
    fut=[r for r in rows if r["market_start_ms"]>CUTOFF_MS]
    dev=base+fut[:DEV_POST]
    if len(dev)!=DEV_EXPECTED: raise RuntimeError(f"Esperadas 211 filas; hay {len(dev)} (base={len(base)}, post50={min(50,len(fut))})")
    return dev,base,fut

def arrays(rows):
    X=np.asarray([r["x"] for r in rows],float)
    up=np.asarray([r["up_realized_pnl"] for r in rows],float)
    dn=np.asarray([r["down_realized_pnl"] for r in rows],float)
    return X,up,dn

def pair(name):
    if name=="direct_pnl_ridge":
        def one(): return Pipeline([("imputer",SimpleImputer(strategy="median",add_indicator=True,keep_empty_features=True)),("scale",StandardScaler()),("model",Ridge(alpha=100.0))])
    elif name=="direct_pnl_hist_gradient_boosting":
        def one(): return Pipeline([("imputer",SimpleImputer(strategy="median",add_indicator=True,keep_empty_features=True)),
            ("model",HistGradientBoostingRegressor(learning_rate=0.05,max_iter=100,max_depth=2,min_samples_leaf=20,l2_regularization=5.0,random_state=980))])
    else: raise ValueError(name)
    return one(),one()

def fit_pair(name,rows):
    X,u,d=arrays(rows); um,dm=pair(name); um.fit(X,u); dm.fit(X,d); return {"name":name,"up_model":um,"down_model":dm}

def predict(bundle,rows):
    X,_,_=arrays(rows); return np.asarray(bundle["up_model"].predict(X),float),np.asarray(bundle["down_model"].predict(X),float)

def oof(name,rows):
    n=len(rows); pu=np.full(n,np.nan); pd=np.full(n,np.nan); start=MIN_TRAIN
    while start<n:
        end=min(n,start+BLOCK); b=fit_pair(name,rows[:start]); u,d=predict(b,rows[start:end]); pu[start:end]=u; pd[start:end]=d; start=end
    mask=np.isfinite(pu)&np.isfinite(pd); return pu,pd,mask

def corr(a,b):
    if len(a)<2: return None
    a=np.asarray(a,float); b=np.asarray(b,float)
    if np.std(a)==0 or np.std(b)==0: return None
    return float(np.corrcoef(a,b)[0,1])

def trades(rows,pu,pd,t):
    out=[]
    for r,u,d in zip(rows,pu,pd):
        side="Up" if u>=d else "Down"; pred=float(max(u,d))
        if pred<t: continue
        realized=float(r["up_realized_pnl"] if side=="Up" else r["down_realized_pnl"]); c=float(r["up_cost"] if side=="Up" else r["down_cost"])
        out.append({"predicted_pnl":pred,"realized_pnl":realized,"cost":c,"side":side})
    return out

def metrics(ts,t):
    if not ts: return {"threshold":t,"trades":0,"wins":0,"net_pnl":0.0,"roi_on_cost":None,"mean_pnl":None,"predicted_realized_correlation":None,"largest_positive_trade_share":None,"first_half_net_pnl":0.0,"second_half_net_pnl":0.0,"eligible":False}
    pnl=np.asarray([x["realized_pnl"] for x in ts]); pred=np.asarray([x["predicted_pnl"] for x in ts]); c=np.asarray([x["cost"] for x in ts])
    pos=pnl[pnl>0]; pos_total=float(pos.sum()) if len(pos) else 0.0; largest=float(pos.max()) if len(pos) else 0.0; share=largest/pos_total if pos_total>0 else None
    mid=len(ts)//2; first=float(pnl[:mid].sum()); second=float(pnl[mid:].sum()); cr=corr(pred,pnl); roi=float(pnl.sum()/c.sum()) if c.sum() else None
    eligible=bool(len(ts)>=MIN_TRADES and pnl.sum()>0 and roi is not None and roi>0 and cr is not None and cr>0 and first>=0 and second>=0 and share is not None and share<=MAX_POS_SHARE)
    return {"threshold":float(t),"trades":len(ts),"wins":int(np.sum(pnl>0)),"win_rate":float(np.mean(pnl>0)),"net_pnl":float(pnl.sum()),"roi_on_cost":roi,"mean_pnl":float(pnl.mean()),"predicted_realized_correlation":cr,"largest_positive_trade_share":share,"first_half_net_pnl":first,"second_half_net_pnl":second,"eligible":eligible}

def prepare(db):
    verify_impl()
    if PREPARE_JSON.exists() or MODEL_FILE.exists():
        if PREPARE_JSON.exists() and MODEL_FILE.exists():
            r=loadj(PREPARE_JSON); print("V0.9.8 PREPARE YA CONGELADO - NO SE SOBRESCRIBE"); print("SELECCIONADO:",r.get("selected_candidate")); print("THRESHOLD:",r.get("selected_threshold")); print("VEREDICTO:",r.get("verdict")); return
        raise RuntimeError("Existe solo uno de PREPARE/MODEL; revisar sin borrar")
    all_rows,costs=load_rows(db); dev,base,fut=dev_split(all_rows); names=["direct_pnl_ridge","direct_pnl_hist_gradient_boosting"]; results={}; finals={}
    for name in names:
        pu,pd,mask=oof(name,dev); rr=[r for r,k in zip(dev,mask) if k]; pu,pd=pu[mask],pd[mask]
        grid=[metrics(trades(rr,pu,pd,t),t) for t in GRID]; elig=[m for m in grid if m["eligible"]]
        chosen=max(elig,key=lambda m:(m["net_pnl"],m["mean_pnl"],m["threshold"])) if elig else None
        results[name]={"oof_rows":int(mask.sum()),"threshold_grid":grid,"selected_threshold":None if chosen is None else chosen["threshold"],"selected_metrics":chosen,"candidate_eligible":chosen is not None}
        finals[name]=fit_pair(name,dev)
    elig=[(n,r) for n,r in results.items() if r["candidate_eligible"]]; selected=None; selected_t=None
    if elig:
        simplicity={"direct_pnl_ridge":0,"direct_pnl_hist_gradient_boosting":1}
        selected,res=max(elig,key=lambda x:(x[1]["selected_metrics"]["net_pnl"],x[1]["selected_metrics"]["mean_pnl"],-simplicity[x[0]]))
        selected_t=res["selected_threshold"]
    verdict="CANDIDATE_FOUND_DEVELOPMENT_ONLY" if selected else "FAIL_ALL_DIRECT_PNL_CANDIDATES"
    bundle={"schema":"modelo_v098_direct_pnl","created_at":now(),"development_rows":len(dev),"feature_names":FEATURES,"selected_candidate":selected,"selected_threshold":selected_t,"models":finals,"prereg_sha256":sha(PREREG),"implementation_sha256":sha(IMPL)}
    joblib.dump(bundle,MODEL_FILE)
    report={"schema":"prepare_v098_direct_pnl","created_at":now(),"development_rows":len(dev),"base_rows":len(base),"post_cutoff_rows_used_for_development":50,"future_rows_available_but_not_used_beyond_first50":max(0,len(fut)-50),"oof_rows":len(dev)-MIN_TRAIN,"results":results,"selected_candidate":selected,"selected_threshold":selected_t,"verdict":verdict,"model_file":str(MODEL_FILE),"model_sha256":sha(MODEL_FILE),"costs":costs,"money_real":"BLOQUEADO"}
    PREPARE_JSON.write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    print("="*92); print("V0.9.8 DIRECT-PNL - PREPARE OOF"); print("="*92)
    print("Desarrollo:",len(dev),"(base",len(base),"+ primeras 50 post-cutoff)"); print("OOF:",len(dev)-MIN_TRAIN); print("Filas futuras 51+ existentes pero NO usadas:",max(0,len(fut)-50)); print()
    for n in names:
        m=results[n]["selected_metrics"]
        if m is None: print(f"{n:42s} -> SIN THRESHOLD ELEGIBLE")
        else: print(f"{n:42s} -> thr={m['threshold']:.2f} trades={m['trades']:3d} PnL={m['net_pnl']:+.5f} ROI={m['roi_on_cost']:+.4f} corr={m['predicted_realized_correlation']:+.4f}")
    print(); print("SELECCIONADO:",selected); print("THRESHOLD:",selected_t); print("VEREDICTO:",verdict); print("Modelo:",MODEL_FILE); print("Reporte:",PREPARE_JSON); print("DINERO REAL: BLOQUEADO"); print("="*92)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--db",default=str(DB_DEFAULT)); g=ap.add_mutually_exclusive_group(required=True); g.add_argument("--freeze-implementation",action="store_true"); g.add_argument("--prepare",action="store_true"); a=ap.parse_args()
    if a.freeze_implementation: freeze_impl(); return
    db=Path(a.db).expanduser().resolve()
    if not db.exists(): raise SystemExit(f"No existe DB: {db}")
    prepare(db)

if __name__=="__main__": main()
