"""Register / advance the two paired Phase 3D shadow books using local receipts."""
import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from contextlib import contextmanager
from datetime import date,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.integrity import require_runtime_guard,sha256_file,verify_frozen_locks
from research.pit.valuediv_shadow import CAPITALS,VALUE,MODELS,OUTPUT,PIT,VERDICT_HASH,encode,require


@contextmanager
def registered_runtime():
    """Resolve the original registration's relative paths without changing the signed ledger or its identity."""
    prior=ROOT/"reports/phase3_pit_verdict/pre_fractional/pit_verdict_manifest.json"
    require(sha256_file(prior)==VERDICT_HASH,"原 Step 3C 清单 SHA-256 不符")
    receipt=json.loads(prior.read_bytes())
    legacy_file=ROOT/"archive/phase3c_v1/pit_backtest_engine.py"
    engine_key="research/pit/pit_backtest_engine.py"
    require(sha256_file(legacy_file)==receipt["source_sha256"][engine_key],"原整手研究引擎 SHA-256 不符")
    ledger_file=ROOT/"research/pit/valuediv_shadow.py"
    require(sha256_file(ledger_file)=="ecfa238d8719e40cd92c5407d1708e83859281906af5e46084c539366633b3dd",
            "已登记影子引擎 SHA-256 不符")
    from archive.phase3c_v1 import pit_backtest_engine as legacy
    verify_frozen_locks(ROOT)
    # rule_identity in the immutable module checks both strategy files under one root.
    # Give that verifier a private directory containing the exact bytes it registered.
    with TemporaryDirectory() as tmp:
        root=Path(tmp)
        files={name:ROOT/name for name in (*receipt["frozen_after"],"core/factor_court.py",
                                           "data/verified/calendar_lookahead.csv")}
        files[engine_key]=legacy_file
        for name,source in files.items():
            target=root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
        spec=importlib.util.spec_from_file_location("registered_phase3d_shadow",ledger_file)
        shadow=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shadow)
        shadow.ROOT=root
        shadow.PRIOR=prior
        shadow.quantity=legacy.quantity
        shadow.trim_quantity=legacy.trim_quantity
        shadow.rule_identity()
        yield shadow,legacy


def write_rehearsal_packet(root,day,parts):
    directory=root/day
    directory.mkdir(parents=True,exist_ok=True)
    refs={}
    for role,data in parts.items():
        name=role+".json"
        blob=encode(data).encode()
        (directory/name).write_bytes(blob)
        refs[role]=dict(file=name,sha256=hashlib.sha256(blob).hexdigest())
    path=directory/"session.json"
    path.write_text(encode(dict(format="valuediv-session-v1",kind="historical_validation",day=day,sections=refs)),
                    encoding="utf8")
    return path


def historical_parts(t,i,coverage):
    day,prev=t.days[i],t.days[i-1]
    month=day[:7]
    prev_month=(date.fromisoformat(day).replace(day=1)-timedelta(days=1)).isoformat()[:7]
    calendar=[d for d in t.days if prev_month<=d[:7]<=month]
    calendar.append(next(d for d in t.days if d[:7]>month))
    first=next(d for d in calendar if d[:7]==month)
    observed=calendar[calendar.index(first)-1]
    codes=[t.codes[j] for j in np.flatnonzero(t.member[i])]
    coverage.update(codes)
    opening,closing={},{}
    p=t.panel
    for c in sorted(coverage):
        j=t.codes.index(c)
        active=bool(t.opened[i,j])
        ref=float(p["pre_close"][i,j]) if active else t.refmark[i,j]/100
        if not ref and c not in codes:
            continue
        opening[c]=dict(status="open" if active else "suspended",open=float(p["open"][i,j]) if active else None,
                        up_limit=float(p["up_limit"][i,j]) if active else None,down_limit=float(p["down_limit"][i,j]) if active else None,
                        limit="bounded",pre_close=ref,industry=t.industry[j])
        closing[c]={k:float(p[k][i,j]) if active else None for k in ("close","high","low")}
    events=[]
    for j,items in t.events.get(i,{}).items():
        for k,e in enumerate(items):
            events.append(dict(type="ex",id=f"{day}/{k}",code=t.codes[j],announced=date.fromisoformat(e["imp_ann_date"]).isoformat(),
                               date=day,holder_cash=e["holder_cash"],ref_cash=e["ref_cash"],bonus=e["bonus"],
                               pay_date=date.fromisoformat(e["pay_date"]).isoformat() if e["pay_date"] else None,
                               share_date=date.fromisoformat(e["div_listdate"]).isoformat() if e["div_listdate"] else None))
    for j in t.terminal.get(i,()):
        code=t.codes[j]
        meta=p["corporate_actions"][code]["delisting"]
        last=t.session(meta["last_open"])
        events.append(dict(type="delist",id="historical-terminal/"+code,code=code,announced=prev,date=day,
                           last_open=t.days[last],last_close=float(p["close"][last,j])))
    require(not t.transfer_credit.get(i),"此历史验收切片含需单独生成过户包的事件")
    def source(at,data):
        return dict(known_at=at+"+08:00",source="历史工程验收；时钟模拟，原数据为 Phase 3B 签发快照",data=data)
    parts=dict(calendar=source(prev+"T08:00:00",dict(sessions=calendar)),
               membership=source(first+"T08:00:00",dict(observed=observed,effective=first,codes=codes)),
               opening=source(day+"T09:30:00",dict(day=day,rows=opening)),
               closing=source(day+"T15:01:00",dict(day=day,rows=closing)),
               actions=source(day+"T08:00:00",dict(day=day,events=events)))
    if day in t.month_signals:
        vals={}
        for j in np.flatnonzero(t.member[i] & t.opened[i]):
            vals[t.codes[j]]=({k:float(p[k][i-1,j]) if np.isfinite(p[k][i-1,j]) else None for k in ("pe_ttm","dv_ttm")}
                              if t.basic[i-1,j] else None)
        parts["factors"]=source(prev+"T20:00:00",dict(observation_date=prev,rows=vals))
    return parts


def verify_offline(output=OUTPUT):
    with registered_runtime() as (shadow,legacy):
        return verify_registered_slice(shadow,legacy,output)


def verify_registered_slice(shadow,legacy,output):
    t=legacy.Tape()
    p=legacy.Pool(t,t.member)
    lo,hi=t.days.index("2024-06-17"),t.days.index("2024-08-09")
    cutoff=t.days[lo-1]
    span=t.days[lo],t.days[hi]
    proof=[]
    with TemporaryDirectory() as temp:
        work=Path(temp)
        path=work/"rehearsal.sqlite3"
        ledger=shadow.Ledger(path,rehearsal_start=cutoff)
        coverage=set()
        try:
            for i in range(lo,hi+1):
                packet=write_rehearsal_packet(work,t.days[i],historical_parts(t,i,coverage))
                require(ledger.append(packet),"首次入账失败")
                if i==(lo+hi)//2:
                    ledger.db.close()
                    ledger=shadow.Ledger(path,rehearsal_start=cutoff)
                    require(not ledger.append(packet),"重启后的重复日包产生写入")
            for capital in CAPITALS:
                for m in MODELS:
                    model=legacy.Model(m,kv=3) if m==VALUE else legacy.Model(m,equal=True)
                    b=legacy.replay(t,p,model,span,capital,keep_fills=True)
                    bind=(capital,m)
                    rows=ledger.db.execute("SELECT nav_cents,cash_cents,lot_idle_mean_pct,estate_total_cents FROM daily WHERE capital=? AND model=? ORDER BY day",bind).fetchall()
                    np.testing.assert_array_equal([r[0] for r in rows],b["nav"])
                    np.testing.assert_array_equal([r[1] for r in rows],b["cash"])
                    actual=ledger.db.execute("SELECT day,code,side,shares,price_cents,commission_cents,stamp_cents,cash_after_cents FROM fills WHERE capital=? AND model=? ORDER BY rowid",bind).fetchall()
                    expected=[(day,t.codes[c],"buy" if side=="买" else "sell",n,px,commission,tax,cash)
                              for day,_,c,side,n,px,commission,tax,cash,_ in b["fills"]]
                    require(actual==expected,f"历史成交不一致 {bind}")
                    np.testing.assert_allclose(rows[-1][2],b["idle"],rtol=0,atol=1e-12)
                    estate=sum(n for (_,j),n in b["pnl"].items() if any(word in t.industry[j] for word in ("地产","房产")))
                    require(estate==rows[-1][3],"历史地产股归因不符")
                    proof.append(dict(capital=capital,model=m,sessions=len(rows),fills=len(actual),last_nav_cents=rows[-1][0],
                                      net_estate_pnl_cents=estate,lot_idle_mean_pct=rows[-1][2]))
            record=dict(kind="historical_validation",first=span[0],last=span[1],price_and_factor_data="实际历史签发快照",
                        availability_clock="验收模拟；不证明历史披露时点",post_freeze_observations=0,
                        restart_and_duplicate_verified=True,matched="逐日权益、现金、逐笔数量价格费税与余额、期末地产盈亏及整手闲置",
                        accounts=proof,entitlement_rows=ledger.db.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0],
                        pit_manifest_sha256=sha256_file(PIT/"pit_manifest.json"),
                        shadow_engine_sha256=sha256_file(ROOT/"research/pit/valuediv_shadow.py"))
        finally:
            ledger.db.close()
    output.mkdir(parents=True,exist_ok=True)
    (output/"offline_acceptance.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf8",newline="\n")
    return record


def main():
    require_runtime_guard()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-offline",action="store_true",help="在临时数据库中用历史真实行情对账")
    parser.add_argument("--input",type=Path,nargs="+",help="按交易日排序的本地前瞻 session.json")
    args=parser.parse_args()
    with registered_runtime() as (shadow,legacy):
        if args.verify_offline:
            result=verify_registered_slice(shadow,legacy,OUTPUT)
            print("历史增量验收通过",result["first"],result["last"],sum(r["fills"] for r in result["accounts"]),"笔成交")
        ledger=shadow.Ledger(OUTPUT/"shadow.sqlite3")
        try:
            for path in args.input or ():
                print(path,"已入账" if ledger.append(path) else "相同日包，已入账")
        finally:
            try:
                status=shadow.export(ledger,OUTPUT)
                print(status["status"],"正式会话",status["observed_sessions"],"成交",status["fills"],"最后交易日",status["last_session"])
            finally:
                ledger.db.close()


if __name__=="__main__":
    main()

