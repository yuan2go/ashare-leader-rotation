"""Phase 5A offline verification, local ingestion and research exports."""
import argparse
import csv
import io
import json
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.integrity import require_runtime_guard
from engine import rounded
from research.pit import forward_session as fs, multi_shadow_ledger as ledger
from scripts import run_phase4a_research as phase4a, run_phase4b_portfolio as phase4b

LABELS = {ledger.DEF:"[LIVE CANDIDATE · 稳健]", ledger.E4M:"[LIVE CANDIDATE · 主力]",
          ledger.C3:"[OBSERVATION ONLY · 探索盘勿实盘重仓]"}
TICKET_FIELDS = ("account","role_tag","risk","state","provenance_mode","signal_day","opening_day","next_signal_day",
                 "m200_state","m200_deviation_pct","code","name","asset","side","reason","proxy_price_rmb",
                 "shares","target_weight_pct","estimated_cash_rmb")


def csv_file(path, fields, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    fs.atomic(path, buffer.getvalue().encode("utf-8-sig"))


def protected():
    prior = fs.load(ledger.PHASE4C/"phase4c_manifest.json")
    paths = set(prior["protected_before"])
    paths.update(p.relative_to(fs.ROOT).as_posix() for p in ledger.ETFDIR.iterdir() if p.is_file())
    paths.update(p.relative_to(fs.ROOT).as_posix() for p in ledger.PHASE4C.rglob("*") if p.is_file())
    return {p:fs.digest((fs.ROOT/p).read_bytes()) for p in sorted(paths)}


def manifest_update(out, stage=None, **values):
    path = out/"phase5a_manifest.json"
    manifest = fs.load(path) if path.exists() else {"protected_before":protected()}
    if stage:
        actual = protected()
        fs.require(actual==manifest["protected_before"], f"{stage} 受保护资产哈希分歧")
        manifest[stage] = actual
    manifest.update(values)
    manifest["artifact_sha256"] = {p.relative_to(out).as_posix():fs.digest(p.read_bytes())
                                   for p in sorted(out.rglob("*")) if p.is_file() and p!=path}
    fs.write(path,manifest)


def next_signals(calendar, day):
    result = dict(weekly_defense=None, month_end_etf=None, month_start_core=None)
    for prev,signal,nxt in zip(calendar,calendar[1:],calendar[2:]):
        if signal <= day:
            continue
        if result["weekly_defense"] is None and fs.week(signal)!=fs.week(nxt):
            result["weekly_defense"] = signal
        if result["month_end_etf"] is None and signal[:7]!=nxt[:7]:
            result["month_end_etf"] = signal
        if result["month_start_core"] is None and prev[:7]!=signal[:7]:
            result["month_start_core"] = signal
    return result


def proxy_parts(day, following, parts):
    result = dict(parts)
    opening, closing = {}, {}
    for code,row in parts["stocks_opening"]["rows"].items():
        close = parts["stocks_closing"]["rows"][code]
        mark = close["close"]
        opening[code] = dict(row,open=mark,pre_close=mark or row["pre_close"],up_limit=None,down_limit=None,unbounded=True)
        closing[code] = dict(close,low=mark,high=mark)
    result.update(stocks_opening=dict(day=following,rows=opening),stocks_closing=dict(day=following,rows=closing),
                  stocks_actions=dict(day=following,events=[]),etf_actions=dict(day=following,events=[]),
                  stocks_factors=dict(observation_date=day,rows={}),
                  etf_quotes=dict(day=following,rows={c:dict(r,open=r["close"],pre_close=r["close"] or r["pre_close"])
                                                     for c,r in parts["etf_quotes"]["rows"].items()}))
    index = parts["index_quote"]["row"]
    result["index_quote"] = dict(day=following,row=dict(index,open=index["close"]))
    return result


def tickets(book, latest, signals, calendar, calls):
    out = book.output
    status = latest["provenance_mode"] if latest else ""
    day = latest["day"] if latest else book.cutoff
    following = calendar[calendar.index(day)+1] if day in calendar and calendar[-1]!=day else None
    m200 = signals.get("m200",{})
    active = {ledger.DEF:signals.get("defense_due",False),
              ledger.E4M:any((signals.get("defense_due"),signals.get("attack_due"),m200.get("flip"))),
              ledger.C3:signals.get("core_due",False)}
    labels = LABELS
    v7a = book.profile=="v7a"
    if v7a:
        labels = {key:f"[{ledger.RISK_LABELS[key]}]" for key in (ledger.DEF,ledger.E4M,ledger.V5,ledger.C3)}
        active[ledger.V5] = any((signals.get("defense_due"), signals.get("attack_due"),
                                 signals.get("vol_monitor",{}).get("resize_due")))
    rows = []
    if latest and any(active.values()):
        entries = book.entries()
        proxy = ledger.Market(book.snapshot,book.cutoff,entries+[(following,proxy_parts(day,following,entries[-1][1]))])
        books,attack = proxy.books(450000,include_v5=v7a)
        attacks = attack if v7a else {ledger.E4M:attack}
    else:
        books, attacks, proxy = {},{},None
    for model in labels:
        future = calls["month_start_core"] if model==ledger.C3 else calls["weekly_defense"]
        if model in (ledger.E4M,ledger.V5):
            upcoming = [x for x in (future,calls["month_end_etf"]) if x]
            future = min(upcoming,default=None)
        common = dict(account=model,risk=labels[model],role_tag=ledger.ROLES[model],provenance_mode=status,
                      opening_day=following,next_signal_day=future,m200_state=m200.get("state"),
                      m200_deviation_pct=m200.get("deviation_pct"))
        if v7a:
            common["as_of"] = latest["day"] if latest else None
        if not active[model]:
            rows.append(dict(**common,state="NO_ACTION_DUE"))
            continue
        b = books[model]
        raw = ledger.fills_for(proxy,model,b,attacks.get(model),following)
        attack_signals = [i for i in proxy.attack if i < len(proxy.days)-1]
        selected_etfs = {proxy.etf.codes[j] for j in proxy.attack[max(attack_signals)]} if attack_signals else set()
        orders = []
        for asset,code,side,qty,px,units,_,_,_,_,signal,reason,kind,_ in sorted(raw,key=lambda r:r[2]!="sell"):
            if asset=="etf":
                cap = (signals["vol_monitor"]["effective_attack_cap_pct"] if model==ledger.V5
                       else m200["attack_cap_pct"])
                weight = cap if code in selected_etfs else 0
            else:
                weight = 0 if kind=="exit" else (100/5 if model==ledger.C3 else 50/3)
            orders.append(dict(**common,state="PROXY_ORDER",signal_day=signal,code=code,name=proxy.names.get(code,code),
                               asset=asset,side=side,shares=qty,proxy_price_rmb=px/units,target_weight_pct=weight,
                               estimated_cash_rmb=int(b["cash"][-1])/100,reason=reason))
        rows.extend(orders or [dict(**common,state="SIGNAL_DUE_NO_PROXY_FILL",signal_day=day,
                                   estimated_cash_rmb=int(b["cash"][-1])/100)])
    csv_file(out/"next_open_order_ticket.csv",(*TICKET_FIELDS,"as_of") if v7a else TICKET_FIELDS,rows)
    headline = f"# 次日开盘影子指令单 · {following or '待日历确认'}"
    md = [headline,"",f"收盘观察日 {day}，来源 {status or '尚无正式会话'}。","",
          "按收盘价加减 2bp 估算股数和现金。实际开盘须重新检验停牌、限价与可成交数量。"]
    if m200:
        state = "弱" if m200["state"]=="weak" else "强"
        md.extend(["",f"M200_HYST_HALF 为{state}市，沪深 300 收盘 {m200['close']:.3f}，"
                       f"MA200 {m200['ma200']:.3f}，偏离 {m200['deviation_pct']:+.2f}%。"])
    if "vol_monitor" in signals:
        vol = signals["vol_monitor"]
        monitor_code = vol["target_etf_code"] or vol["reference_etf_code"] or "待选"
        sigma = vol["etf_vol20_ann_pct"]
        shown_sigma = f"{sigma:.2f}%" if sigma is not None else "待足额样本"
        md.extend(["",f"V5 · ETF {monitor_code}，20 日年化波动率 {shown_sigma}，"
                      f"有效进攻目标上限 {vol['effective_attack_cap_pct']:.2f}%。"])
    for model,label in labels.items():
        relevant = [row for row in rows if row["account"]==model]
        md.extend(["",f"## {model} {label}","",
                   f"下一个预定信号日 {relevant[0]['next_signal_day'] or '待后续日历'}。"])
        for row in relevant:
            if row["state"]=="PROXY_ORDER":
                side = "买" if row["side"]=="buy" else "卖"
                md.append(f"- {side} {row['name']} {row['code']}（{row['asset']}），{row['shares']} 股／份 × "
                          f"¥{row['proxy_price_rmb']:.3f}，目标 {row['target_weight_pct']:.2f}%，"
                          f"预计账户现金 ¥{row['estimated_cash_rmb']:,.2f}。")
            else:
                md.append(f"- {row['state']}")
    fs.atomic(out/"next_open_order_ticket.md",("\n".join(md)+"\n").encode())


def export(book):
    # Keep concurrent ingesters and status exports on the same committed version.
    with book.db:
        book.db.execute("BEGIN IMMEDIATE")
        return export_snapshot(book)


def export_snapshot(book):
    out, db = book.output, book.db
    out.mkdir(parents=True,exist_ok=True)
    latest = db.execute("SELECT * FROM sessions ORDER BY day DESC LIMIT 1").fetchone()
    last = latest["day"] if latest else None
    signals = json.loads(latest["signals"]) if latest else {}
    cal = json.loads(latest["parts"])["calendar"]["sessions"] if latest else [
        book.cutoff,*[r["date"] for r in ledger.stock.read_rows(fs.ROOT/"data/verified/calendar_lookahead.csv")]]
    calls = next_signals(cal,last or book.cutoff)
    rows = db.execute("SELECT d.*,s.provenance_mode FROM daily d JOIN sessions s USING(day) ORDER BY day,capital,account").fetchall()
    lookup = {(r["day"],r["capital"],r["account"]):r["nav_cents"] for r in rows}
    comparison = comparison_rows(rows)
    fields = [r["name"] for r in db.execute("PRAGMA table_info(daily)")]
    csv_file(out/"daily_comparison.csv",(*fields,"provenance_mode","return_pct","drawdown_pct","max_drawdown_pct",
                                       "cash_pct","excess_ew_pp","excess_csi300_pp",
                                       "stock_daily_pnl_cents","etf_daily_pnl_cents"),comparison)
    for table,name,condition,params,order in (
        ("fills","fills.csv","",(),"day,capital,account,seq"),
        ("corporate_actions","corporate_actions.csv","",(),"day,capital,account,asset,code,event_id,entry"),
        ("holdings","holdings_latest.csv","WHERE day=?",(last,),"capital,account,asset,code"),
    ):
        cur = db.execute(f"SELECT * FROM {table} {condition} ORDER BY {order}",params)
        rows = [dict(r) for r in cur]
        fields = [col[0] for col in cur.description]
        if table=="holdings":
            fields.append("market_weight_pct")
            for row in rows:
                row["market_weight_pct"] = 100*row["market_value_cents"]/lookup[(last,row["capital"],row["account"])]
        csv_file(out/name,fields,rows)
    counts = dict(db.execute("SELECT provenance_mode,COUNT(*) FROM sessions GROUP BY provenance_mode"))
    state = dict(latest_session=last,strict_live_sessions=counts.get(fs.STRICT,0),catchup_sessions=counts.get(fs.CATCHUP,0),
                 latest_provenance_mode=latest["provenance_mode"] if latest else None,
                 m200=signals.get("m200"),next_signals=calls,accounts=[r for r in comparison if r["day"]==last],
                 total_fills=db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
    if book.profile=="v7a":
        state["vol_monitor"] = signals.get("vol_monitor")
    tickets(book,latest,signals,cal,calls)
    fs.write(out/"shadow_status.json",state)
    return state


def comparison_rows(rows):
    lookup = {(r["day"],r["capital"],r["account"]):r["nav_cents"] for r in rows}
    peaks, dd, prior_pnl, comparison = {},{},{},[]
    for r in rows:
        nav, cap, account, day = r["nav_cents"],r["capital"],r["account"],r["day"]
        k = cap,account
        peaks[k] = max(peaks.get(k,cap*100),nav)
        dd[k] = min(dd.get(k,0),100*(nav/peaks[k]-1))
        previous_stock, previous_etf = prior_pnl.get(k,(0,0))
        prior_pnl[k] = r["stock_pnl_cents"], r["etf_pnl_cents"]
        comparison.append(dict(r, return_pct=100*(nav/(cap*100)-1),drawdown_pct=100*(nav/peaks[k]-1),
                               max_drawdown_pct=dd[k],cash_pct=100*r["cash_cents"]/nav,
                               excess_ew_pp=(nav-lookup[(day,cap,ledger.EW)])/cap,
                               excess_csi300_pp=(nav-lookup[(day,cap,ledger.IDX)])/cap,
                               stock_daily_pnl_cents=r["stock_pnl_cents"]-previous_stock,
                               etf_daily_pnl_cents=r["etf_pnl_cents"]-previous_etf))
    return comparison


def historical_parts(snapshot, i):
    t, e = snapshot.stock, snapshot.etf
    day, previous = t.days[i], t.days[i-1]
    prev_month = (date.fromisoformat(day).replace(day=1)-timedelta(days=1)).isoformat()[:7]
    calendar = [d for d in t.days if prev_month<=d[:7]<=day[:7]]
    calendar.extend([d for d in t.days[i:i+46] if d[:7]>day[:7]][:10])
    _, _, observed, first = fs.calendar_cursor(day,calendar)
    members = [t.codes[j] for j in np.flatnonzero(t.member[i])]
    opening, closing, factors, no_row, events = {},{},{},{},[]
    active = t.opened[i]
    terminal = set(t.terminal.get(i,[]))
    p = t.panel
    for j in np.flatnonzero(t.member[i] | (t.marks[i]>0) | (t.refmark[i]>0)):
        code = t.codes[j]
        status = "open" if active[j] else "delisted" if j in terminal else "suspended"
        up,down = (float(p[k][i,j]) if active[j] and np.isfinite(p[k][i,j]) else None
                   for k in ("up_limit","down_limit"))
        ref = float(p["pre_close"][i,j]) if active[j] else t.refmark[i,j]/100
        opening[code] = dict(open=float(p["open"][i,j]) if active[j] else None,pre_close=ref,
                             status=status,up_limit=up,down_limit=down,unbounded=up is None and down is None,
                             industry=t.industry[j] or "UNKNOWN",name=snapshot.names[code])
        closing[code] = {k:float(p[k][i,j]) if active[j] else None for k in ("high","low","close","vol","amount")}
        if t.basic[i-1,j]:
            factors[code] = {k:float(p[k][i-1,j]) if np.isfinite(p[k][i-1,j]) else None for k in fs.FACTORS}
        elif active[j] and code in members:
            no_row[code] = "signed_snapshot_no_row"
        for k,event in enumerate(t.events.get(i,{}).get(j,())):
            events.append(dict(code=code,type="ex",id=f"{code}/{day}/{k}",date=day,
                               announced=date.fromisoformat(event["imp_ann_date"]).isoformat(),
                               holder_cash=event["holder_cash"],ref_cash=event["ref_cash"],bonus=event["bonus"],
                               pay_date=date.fromisoformat(event["pay_date"]).isoformat() if event["pay_date"] else None,
                               share_date=date.fromisoformat(event["div_listdate"]).isoformat() if event["div_listdate"] else None))
        if j in terminal:
            meta = p["corporate_actions"][code]["delisting"]
            events.append(dict(code=code,type="delist",id=f"{code}/{day}/delist",date=day,announced=None,
                               last_open=date.fromisoformat(meta["last_open"]).isoformat()))
    for j,event in t.transfer_credit.get(i,()):
        code = t.codes[j]
        ratio = (event["transferred_shares"]/event["eligible_shares"] if "eligible_shares" in event else float(event["per_ten_shares"])/10)
        events.append(dict(code=code,type="transfer",id=event["notice_id"],date=day,
                           announced=date.fromisoformat(event["disclosure_date"]).isoformat(),ratio=ratio,
                           record_date=date.fromisoformat(event["record_date"]).isoformat(),
                           share_date=t.days[i+1]))
    etfs, fund_events = {},[]
    for j,code in enumerate(e.codes):
        q = snapshot.funds
        row = {k:float(q[k][i,j]) if np.isfinite(q[k][i,j]) else None for k in (*fs.OHLC,"vol","amount","adj_factor")}
        row["status"] = "open" if e.opened[i,j] else "suspended"
        if not e.opened[i,j]:
            row["pre_close"] = e.marks[i,j]/1000
        etfs[code] = row
        for k,event in enumerate(snapshot.etf_actions[code]):
            if event["ex_date"] != day:
                continue
            common = dict(code=code,id=f"{code}/{day}/{k}",date=day,announced=event.get("ann_date"), type=event["type"])
            if event["type"]=="split":
                fund_events.append(dict(**common,numerator=event["ratio_numerator"],denominator=event["ratio_denominator"]))
            else:
                fund_events.append(dict(**common,cash=float(event["div_cash"]),pay_date=event["pay_date"]))
    return dict(calendar=dict(sessions=calendar),membership=dict(observed=observed,effective=first,codes=members),
                stocks_opening=dict(day=day,rows=opening),stocks_closing=dict(day=day,rows=closing),
                stocks_actions=dict(day=day,events=events),stocks_factors=dict(observation_date=previous,rows=factors,no_row=no_row),
                index_quote=dict(day=day,code=fs.INDEX,row=dict(open=t.index_open[i],close=t.index_close[i],
                                 pre_close=t.index_close[i-1],high=None,low=None)),
                etf_quotes=dict(day=day,rows=etfs),etf_actions=dict(day=day,events=fund_events))


@contextmanager
def forbid_http():
    """Block outbound sockets even if a future dependency imports a transport."""
    state = {"active":True,"attempts":0}
    def guard(event, args):
        if state["active"] and event in ("socket.connect","socket.getaddrinfo","socket.sendto"):
            state["attempts"] += 1
            raise RuntimeError("Phase 5A 离线验收禁止联网")
    sys.addaudithook(guard)
    try:
        yield state
    finally:
        state["active"] = False


def parity_rows(db, stock_tape, fund_tape, model, cap, expected, attack=None):
    bind = model,cap
    daily = db.execute("SELECT nav_cents,cash_cents FROM daily WHERE account=? AND capital=? ORDER BY day",bind).fetchall()
    diff = np.asarray([tuple(row) for row in daily])-np.column_stack((expected["nav"],expected["cash"]))
    maximum = int(np.abs(diff).max())
    fs.require(maximum==0, f"{bind} 逐日净值或现金误差 {maximum} 分，首个分歧 {np.argwhere(diff != 0)[:1]}")
    fills = db.execute("""SELECT day,asset,code,side,shares,price_units,commission_cents,stamp_cents,slippage_cents
                          FROM fills WHERE account=? AND capital=? ORDER BY day,seq""",bind).fetchall()
    reference = []
    for day,_,j,side,n,px,comm,tax,_,_ in expected.get("fills",()):
        i = stock_tape.days.index(day)
        slip = abs(rounded(n*px)-rounded(n*int(stock_tape.openmark[i,j])))
        reference.append((day,"stock",stock_tape.codes[j],"buy" if side=="买" else "sell",n,px,comm,tax,slip))
    if attack:
        for day,j,side,n,px,comm,tax,_ in attack["fills"]:
            i = fund_tape.days.index(day)
            slip = abs(ledger.etf.cent_notional(px,n)-ledger.etf.cent_notional(int(fund_tape.opens[i,j]),n))
            reference.append((day,"etf",fund_tape.codes[j],side,n,px,comm,tax,slip))
    reference.sort(key=lambda r:r[0])
    actual = [tuple(r) for r in fills]
    fs.require(actual==reference,f"{bind} 成交逐笔分歧，原批量 {len(reference)} 笔，SQLite {len(actual)} 笔")
    return dict(account=model,role_tag=ledger.ROLES[model],capital=cap,sessions=len(daily),
                fills=len(fills),max_nav_diff_cents=maximum,max_cash_diff_cents=maximum)


def verify(out=fs.OUT, *, snapshot=None, profile="v5a"):
    with forbid_http() as network:
        before = protected()
        snapshot = snapshot or ledger.Snapshot()
        t,e = snapshot.stock,snapshot.etf
        # The September weekly flip and December 510050 dividend bracket this slice.
        window = "2024-09-02","2024-12-20"
        lo,hi = [t.days.index(day) for day in window]
        cutoff = t.days[lo-1]
        source = "Phase 4B 股票 PIT、Phase 4C ETF PIT 与签发沪深300开收盘"
        references = {n:dict(source=source,captured_at=None,server_date=None,receipts=[]) for n in fs.SECTIONS}
        proof = []
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            v5_ticket_verified = False
            dbpath = work/"shadow.sqlite3"
            paths = [fs.publish(work/t.days[i],t.days[i],historical_parts(snapshot,i),references,fs.HISTORY)
                     for i in range(lo,hi+1)]
            def open_book():
                return ledger.Ledger(dbpath,output=work,snapshot=snapshot,cutoff=cutoff,historical=True,profile=profile)
            def blocked(callback, contains):
                try:
                    callback()
                except (ValueError,ledger.sqlite3.IntegrityError) as exc:
                    fs.require(contains in str(exc), f"未命中预期保护 {contains}，实际错误 {exc}")
                else:
                    raise AssertionError(f"未拦截 {contains}")
            with open_book() as book:
                blocked(lambda:book.append(paths[1]),"漏日")
                section = paths[0].parent/"stocks_closing.json"
                original = section.read_bytes()
                section.write_bytes(original+b" ")
                blocked(lambda:book.append(paths[0]),"哈希不符")
                section.write_bytes(original)
                fail_account = ledger.V5 if profile=="v7a" else ledger.C3
                book.db.execute(f"""CREATE TRIGGER fail_account BEFORE INSERT ON daily
                    WHEN NEW.account='{fail_account}' BEGIN SELECT RAISE(ABORT,'测试全日回滚'); END""")
                blocked(lambda:book.append(paths[0]),"测试全日回滚")
                for name in ("sessions","daily","fills","holdings","corporate_actions"):
                    fs.require(book.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]==0,f"{name} 留下回滚残行")
                book.db.execute("DROP TRIGGER fail_account")
                for path in paths[:len(paths)//2]:
                    fs.require(book.append(path),"历史日包首次提交失败")
                    if profile=="v7a" and path.parent.name=="2024-09-30":
                        export(book)
                        with (work/"next_open_order_ticket.csv").open(encoding="utf-8-sig",newline="") as file:
                            orders = [r for r in csv.DictReader(file) if r["account"]==ledger.V5 and r["asset"]=="etf"]
                        fs.require(orders and all(
                            r["role_tag"]==ledger.ROLES[ledger.V5] and r["state"]=="PROXY_ORDER"
                            and r["opening_day"]=="2024-10-08" and int(r["shares"])%100==0
                            and float(r["target_weight_pct"])>0 for r in orders
                        ),"V5 月末 ETF 整手试算、开盘日或风险水印失配")
                        v5_ticket_verified = True
            with open_book() as book:
                path = paths[len(paths)//2-1]
                fs.require(not book.append(path),"重启后的重复请求产生写入")
                manifest = path.read_bytes()
                path.write_bytes(manifest+b" ")
                blocked(lambda:book.append(path),"已入账日包内容变更")
                path.write_bytes(manifest)
                for path in paths[len(paths)//2:]:
                    fs.require(book.append(path),"历史日包首次提交失败")
                    if path.parent.name=="2024-12-02":
                        export(book)
                        with (work/"next_open_order_ticket.csv").open(encoding="utf-8-sig",newline="") as ticket:
                            core = [r for r in csv.DictReader(ticket) if r["account"]==ledger.C3]
                        fs.require(core and all(r["role_tag"]==ledger.ROLES[ledger.C3] and
                                   r["state"]=="PROXY_ORDER" and r["opening_day"]=="2024-12-03" for r in core),
                                   "C3 月初实际代理指令或观察水印缺失")
                f = phase4a.Features(t)
                mask = f.pools[phase4b.PRIMARY]
                pool = ledger.stock.Pool(t,mask)
                models = {c.key:c for c in phase4b.candidates(ledger.phase4c.read_json(phase4b.LOCK),phase4b.PRIMARY)}
                defense = {"VALUEDIV_LOWTURN":{i:phase4b.composite_order(t,i,mask) for i in f.month_start if i>=20}}
                schedule = phase4b.schedule_for(models["C3"],f,defense,{})
                etf_schedule = e.schedule(fs.ETF,ledger.ETF_MODEL.signal)
                vol_triggers = {}
                if profile=="v7a":
                    study = ledger.phase6a.Study()
                    signed = ledger.phase6a
                    clock = study.clock(signed.V5,"ETF5","W1",ledger.CAPITALS[0],"2bp")
                for cap in ledger.CAPITALS:
                    d = ledger.stock.replay(t,pool,models["DEF50"].model,window,cap,keep_fills=True,
                                            defense_signals=f.weekly,collect_sleeve_nav=True)
                    c = ledger.stock.replay(t,pool,models["C3"].model,window,cap,keep_fills=True,schedule=schedule)
                    eq = ledger.stock.replay(t,pool,models["PROP"].model,window,cap,keep_fills=True)
                    index = ledger.index_book(t,window,cap)
                    parked = cap//2*100
                    available = d["cash"].copy()
                    for hit in d["delist"]:
                        available[t.session(hit["settlement_session"])-lo] -= hit["settled_cents"]
                    attack = ledger.etf.replay(e,etf_schedule,window,cap//2,timing=ledger.ETF_MODEL.timing,
                                               detail=True,cash_floor=np.maximum(0,parked-available))
                    combined = dict(nav=d["nav"]-parked+attack["nav"],cash=d["cash"]-parked+attack["cash"],fills=d["fills"])
                    for model,expected in ((ledger.DEF,d),(ledger.E4M,combined),(ledger.C3,c),(ledger.EW,eq),(ledger.IDX,index)):
                        proof.append(parity_rows(book.db,t,e,model,cap,expected,attack if model==ledger.E4M else None))
                    values = book.db.execute("SELECT stock_pnl_cents FROM daily WHERE account='E4M' AND capital=? ORDER BY day",(cap,))
                    np.testing.assert_array_equal([r[0] for r in values],d["sleeve_nav"][ledger.stock.DEF]-parked)
                    if profile=="v7a":
                        vol = signed.etf_book(e,study.schedules["ETF5"],window,cap//2,
                                              reserve=np.maximum(0,parked-available),
                                              slip=2,delay=0,model=signed.V5,clock=clock)
                        combined_vol = dict(nav=d["nav"]-parked+vol["nav"],cash=d["cash"]-parked+vol["cash"],fills=d["fills"])
                        proof.append(parity_rows(book.db,t,e,ledger.V5,cap,combined_vol,vol))
                        values = book.db.execute("SELECT stock_pnl_cents FROM daily WHERE account='V5' AND capital=? ORDER BY day",(cap,))
                        np.testing.assert_array_equal([r[0] for r in values],d["sleeve_nav"][ledger.stock.DEF]-parked)
                        vol_triggers[cap] = len(vol["triggers"])
                        expected_counts = np.zeros(hi-lo+1,dtype=int)
                        for _, execution, _, _ in vol["triggers"]:
                            expected_counts[execution-lo:] += 1
                        monitors = [json.loads(r[0])["vol_monitor"] for r in
                                    book.db.execute("SELECT signals FROM sessions ORDER BY day")]
                        np.testing.assert_array_equal([row["scaling_trigger_count"] for row in monitors],expected_counts)
                        np.testing.assert_array_equal([row["applied_attack_cap_pct"] for row in monitors],vol["targets"])
                months_end = [t.days[i] for i in sorted(f.month_end) if lo<=i<=hi]
                months_start = [t.days[i] for i in sorted(f.month_start) if lo<=i<=hi]
                flips = [t.days[b] for a,b in zip(t.week_idx,t.week_idx[1:]) if lo<=b<=hi and t.weak[a]!=t.weak[b]]
                actions = dict(book.db.execute("SELECT asset,COUNT(*) FROM corporate_actions WHERE entry='dividend_receivable' GROUP BY asset"))
                fs.require(hi-lo+1>=65 and len(months_end)>=2 and len(months_start)>=2 and flips,"验收区间不合格")
                fs.require(actions.get("stock") and actions.get("etf"),"验收窗口未实际应计股票及 ETF 分红")
                state = export(book)
                fs.require(len(state["accounts"])==(12 if profile=="v7a" else 10),"验收产物账户数不足")
                if profile=="v7a":
                    fs.require(hi-lo+1==73 and all(vol_triggers.values()) and v5_ticket_verified,
                               "缺少 73 日窗口、V5 缩放或指令单证据")
                    fs.require(not book.db.execute("SELECT 1 FROM fills WHERE asset='etf' AND stamp_cents<>0").fetchone(),
                               "ETF 印花税非零")
        result = dict(status="verified",kind=fs.HISTORY,first=window[0],last=window[1],sessions=hi-lo+1,
                      window_reason="覆盖完整月初月末信号、M200 周频翻转和实际持仓的股票、ETF 分红",
                      batch_and_journal_share_signed_execution_kernels=True, source=source,
                      index_snapshot_fields=["open","close"],month_ends=months_end,month_starts=months_start,
                      m200_weekly_flips=flips,dividend_receivable_entries=actions,accounts=proof,
                      total_fills=sum(r["fills"] for r in proof),maximum_nav_difference_cents=0,
                      maximum_cash_difference_cents=0,defense_sleeve_exact=True,
                      checks=dict(restart=True,idempotent=True,section_tamper=True,changed_session=True,missing_day=True,
                                  rollback=True,core_order_ticket=True),
                      http_requests=network["attempts"])
        if profile=="v7a":
            result.update(market_http_requests=network["attempts"],v5_scaling_trigger_counts=vol_triggers)
            result["checks"].update(v5_defense_exact=True,v5_targets_and_triggers_exact=True,etf_zero_stamp=True,
                                    v5_etf_proxy_ticket=True)
        fs.require(result["http_requests"]==0 and protected()==before,"离线联网或签发资产变更")
        out.mkdir(parents=True,exist_ok=True)
        fs.write(out/"offline_acceptance.json",result)
        return result


def main():
    require_runtime_guard()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify-offline",action="store_true")
    action.add_argument("--input",nargs="+",type=Path)
    action.add_argument("--ingest-ready",action="store_true")
    action.add_argument("--status",action="store_true")
    action.add_argument("--healthcheck",action="store_true")
    parser.add_argument("--continuation",action="store_true",help="keep Phase 5A signed assets read-only")
    args = parser.parse_args()
    out = fs.OUT
    if args.continuation:
        from scripts.run_phase7a_shadow import BRIDGE, output_root
        out = output_root()/"legacy_v5a"
        if args.status:
            print(json.dumps(fs.load((out if (out/"shadow_status.json").is_file() else fs.OUT)/"shadow_status.json"),
                             ensure_ascii=False))
            return
    if args.verify_offline:
        result = verify(out)
        manifest_update(out,"protected_after_offline",offline_verify_http_requests=result["http_requests"])
    elif args.healthcheck:
        from tushare_client import local_settings
        result = dict(state="ok",protected_files=len(protected()),source_files=len(ledger.identity()),
                      tushare_configured=all(local_settings(fs.ROOT).values()),http_requests=0)
        manifest = fs.load(fs.OUT/"phase5a_manifest.json")
        fs.require(manifest["protected_before"]==protected(),"受保护文件哈希不符")
        dbpath = out/"shadow_v5a.sqlite3"
        if args.continuation and not dbpath.exists():
            dbpath = fs.OUT/"shadow_v5a.sqlite3"
        if dbpath.exists():
            from contextlib import closing
            with closing(ledger.sqlite3.connect(dbpath.resolve().as_uri()+"?mode=ro",uri=True)) as db:
                result["sqlite_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
                fs.require(result["sqlite_integrity"]=="ok","SQLite 完整性检查失败")
    else:
        with ledger.Ledger(out/"shadow_v5a.sqlite3",output=out) as book:
            if args.continuation:
                for day in BRIDGE:
                    book.append(fs.OUT/"daypacks"/day/"session.json",evidence_root=fs.OUT)
            paths = args.input or (sorted((out/"daypacks").glob("*/session.json")) if args.ingest_ready else [])
            for path in paths:
                try:
                    print(path,"已入账" if book.append(path) else "已入账，内容相同",flush=True)
                except BaseException:
                    export(book)
                    raise
                export(book)
            result = export(book)
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
