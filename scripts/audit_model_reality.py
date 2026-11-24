"""Read-only falsification audit. Run with the repository's Python 3.11 and NumPy."""
import csv
import hashlib
import itertools
import json
import math
import random
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from statistics import correlation, fmean, mean, stdev, variance

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'research')]
import numpy as np
import engine
import exp_50_pool_phase2b_p2 as p2
from research.common import atomic_text
from core.research_ledger import book, native_stop, select_trend
from core.metrics import compute_portfolio_metrics
from core import research_support as shared
from core.integrity import require_runtime_guard, verify_frozen_locks, verify_required_sha_manifest

b, diag, p1 = p2.barbell, p2.diag, p2.p1
OUT = ROOT / 'reports/model_reality_audit'
WINDOWS = {k: diag.WINDOWS[k] for k in ('W1', 'IS', 'OOS', 'W_Bear')}
SPLIT = ('IS', 'OOS')
M200 = 'M200_HYST_HALF'
MODELS = ('BASE', M200)
CAP = p2.CAPITAL * 100
N, STOCK_SEED, TIMING_SEED = 200, 2026112401, 2026112402
CORE = ('engine.py', 'offline/engine.js')
SM = ('cagr_pct', 'sharpe', 'calmar')
TM = ('sharpe', 'calmar', 'max_dd_pct')


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def snapshot():
    from core.manifest import build_consumed_manifest
    files = [*p1.freeze_receipt(diag.read_json(ROOT/'results/data_audit.json')),
             p2.OUT/'p2_ablation_all_runs.csv']
    for window in WINDOWS:
        for model in MODELS:
            directory = p2.OUT / 'runs/50' / window / '2bp' / model
            files.extend((directory/'nav.csv', directory/'trades.csv'))
    return build_consumed_manifest(ROOT, files, __file__)['sha256']


def write_csv(name, rows):
    import io
    fields = list(dict.fromkeys(k for row in rows for k in row))
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fields, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        if not (all(math.isfinite(v) for v in row.values() if isinstance(v, float))):
            raise RuntimeError(name)
        writer.writerow(row)
    atomic_text(OUT / (name + '.csv'), '\ufeff' + buffer.getvalue())


@dataclass
class Context:
    data: dict
    scan: dict
    vol: list
    defense: tuple
    months: set
    macro: dict
    day_index: dict


def run(ctx, window, *, timing=None, kt=2, order=None, selector=None, stop=native_stop, sleeves='both'):
    def decide(day, nav, peak, previous):
        if selector is not None:
            selector.day = day
        weak = bool(timing and timing[day])
        return weak, 25 if weak else 50, None

    if sleeves == 'defense':
        order = {i: () for i in ctx.defense[0]}
    months = set() if sleeves == 'trend' else ctx.months
    return book(ctx.data, (ctx.scan, None, None, ctx.vol), ctx.defense, months, ctx.macro,
                b.Barbell(50, kt), WINDOWS[window], '2bp', risk_budget_hook=decide, stop_at_close=stop,
                trend_planner=selector.select if selector is not None else select_trend, controls=p1.Controls(keep_rank=kt+2),
                trend_dates=set(), overrides=order)


def metrics(path, initial=CAP):
    measures = compute_portfolio_metrics(path, (), initial)
    return {k: measures[k] for k in ('net_pct', 'cagr_pct', 'max_dd_pct', 'sharpe', 'calmar')}


def benchmark(ctx, window, mode):
    return engine.replay(ctx.data, ctx.scan, mode, WINDOWS[window], *shared.CASES['2bp'])


def check_archive(ledger, window, model, record):
    ledger_dir = p2.OUT / 'runs/50' / window / '2bp' / model
    path = ledger['path']
    for name in ('nav', 'trades'):
        if not (digest(ledger_dir / (name+'.csv')) == record[name+'_sha256']):
            raise RuntimeError("check_archive failed: digest(ledger_dir / (name+'.csv')) == record[name+'_sha256']")
    for r, old in zip(path, diag.read_csv(ledger_dir/'nav.csv'), strict=True):
        if not ((r['date'], p1.yuan(r['equity']), p1.yuan(r['cash'])) == (old['date'], old['nav'], old['cash'])):
            raise RuntimeError("check_archive failed: (r['date'], p1.yuan(r['equity']), p1.yuan(r['cash'])) == (old['date'], old['nav'], old['cash'])")
        if not (r['sleeves'] == {b.TREND: json.loads(old['trend_holdings']), b.VALUE: json.loads(old['defense_holdings'])}):
            raise RuntimeError("check_archive failed: r['sleeves'] == {b.TREND: json.loads(old['trend_holdings']), b.VALUE: json.loads(old['defense_holdings'])}")
    for f, old in zip(ledger['orders'], diag.read_csv(ledger_dir/'trades.csv'), strict=True):
        if not ((f['date'], f['signal'], f['code'], f['shares'], p1.yuan(f['price']), p1.yuan(f['commission']),
                p1.yuan(f['stamp']), p1.yuan(f['cash']), p1.yuan(f['slippage']), f['sleeve'], f['side']) == (
                    old['trade_date'], old['signal_date'], old['symbol'], int(old['shares']), old['price'],
                    old['commission'], old['stamp_duty'], old['cash_after_fill'], old['slippage'],
                    {'trend': b.TREND, 'defense': b.VALUE}[old['sleeve']], {'BUY': '买', 'SELL': '卖'}[old['side']])):
            raise RuntimeError("check_archive failed: verification of (f['date'], f['signal'], f['code'], f['shares'], p1.yuan(f['price']), …")
    m = metrics(path)
    for k, other in [('cagr_pct','ann_return_pct'), ('max_dd_pct','max_dd_pct'), ('calmar','calmar'), ('sharpe','sharpe')]:
        if not (abs(m[k]-float(record[other])) < 1e-9):
            raise RuntimeError((window, model, k))


def pnl_ledger(ctx, book):
    flows, basis, shares = Counter(), defaultdict(Fraction), Counter()
    daily, by_date = [], defaultdict(list)
    for fill in book['orders']:
        by_date[fill['date']].append(fill)
    for r in book['path']:
        day = r['date']
        states = ctx.scan['states'][ctx.day_index[day]]
        for key, qty in list(shares.items()):
            action = ctx.data['actions'].get((key[1], day))
            if action:
                flows[key] += engine.rounded(qty*action['div']*100)
                shares[key] += math.floor(qty*action['bonus']+1e-9)
        for f in by_date[day]:
            key = (f['sleeve'], f['code'])
            qty, fees = f['shares'], f['commission']+f['stamp']
            gross = qty*f['price']
            if f['side'] == '买':
                basis[key] += gross+fees
                shares[key] += qty
                flows[key] -= gross+fees
            else:
                basis[key] *= Fraction(shares[key]-qty, shares[key])
                shares[key] -= qty
                flows[key] += gross-fees
                if not shares[key]:
                    del shares[key]
        holdings = {(s,c): n for s,h in r['sleeves'].items() for c,n in h.items()}
        if not (holdings == shares):
            raise RuntimeError('pnl_ledger failed: holdings == shares')
        marks = {key: n*engine.rounded(states[key[1]]['mark']*100) for key,n in holdings.items()}
        total = {key: value + marks.get(key, 0) for key,value in flows.items()}
        trend = sum(v for (s,c),v in total.items() if s == b.TREND)
        if not (CAP+sum(total.values()) == r['equity']):
            raise RuntimeError(day)
        daily.append((trend, sum(total.values())-trend))
    trend_stocks = {}
    for (s,c), net in total.items():
        if s == b.TREND:
            floating = marks.get((s,c),0) - basis[s,c]
            trend_stocks[c] = (net, float(net-floating), float(floating))
    if not (sum(row[0] for row in trend_stocks.values()) == daily[-1][0]):
        raise RuntimeError('pnl_ledger failed: sum(row[0] for row in trend_stocks.values()) == daily[-1][0]')
    return daily, trend_stocks


def attr_rows(window, model, book, daily):
    path = book['path']
    def slice_row(label, first, end):
        initial = path[first-1]['equity'] if first else CAP
        segment = path[first:end+1]
        m = metrics(segment, initial)
        row = dict(period=label, model=model, first=segment[0]['date'], last=segment[-1]['date'],
                   days=len(segment), opening_nav=initial/100, closing_nav=segment[-1]['equity']/100, **m)
        if daily is not None:
            before = daily[first-1] if first else (0,0)
            delta = segment[-1]['equity']-initial
            years = (date.fromisoformat(row['last'])-date.fromisoformat(row['first'])).days/365.25
            scale = m['cagr_pct']/delta if delta else 100/initial/years
            for i,k in enumerate(('trend','defense')):
                cents = daily[end][i]-before[i]
                row[k+'_pnl_rmb'] = cents/100
                row[k+'_cagr_pp'] = cents*scale
            if not (abs(row['trend_cagr_pp']+row['defense_cagr_pp']-m['cagr_pct']) < 1e-9):
                raise RuntimeError("attr_rows failed: abs(row['trend_cagr_pp']+row['defense_cagr_pp']-m['cagr_pct']) < 1e-9")
        return row
    rows = [slice_row(window, 0, len(path)-1)]
    if window == 'W1':
        for year, group in itertools.groupby(range(len(path)), lambda i: path[i]['date'][:4]):
            ids = list(group)
            rows.append(slice_row(year, ids[0], ids[-1]))
    return rows


def market_states(ctx):
    hist = {c: deque(maxlen=60) for c in ctx.data['universe']}
    weekly, vols = {}, [dict() for _ in ctx.data['days']]
    for i,(day,states) in enumerate(zip(ctx.data['days'], ctx.scan['states'], strict=True)):
        scores = {}
        for c,s in states.items():
            if not s['opened']:
                continue
            hist[c].append(s['mark']*s['factor'])
            if day not in ctx.data['week_ends'] or not s['warm']:
                continue
            prices = list(hist[c])
            score = engine.rounded(1e10*(engine.regression(prices,20)+engine.regression(prices,60))/2)/1e10
            logs = list(map(math.log, prices[-21:]))
            vol = stdev(y-x for x,y in zip(logs, logs[1:]))*math.sqrt(252)
            if s['gate']:
                if not (score == s['score'] and vol == ctx.vol[i][c]):
                    raise RuntimeError("market_states failed: score == s['score'] and vol == ctx.vol[i][c]")
            scores[c], vols[i][c] = score, vol
        if day in ctx.data['week_ends']:
            weekly[i] = sorted(scores, key=lambda c:(scores[c],c))
    return weekly, vols


def hysteresis(ctx, ma, band):
    history, result, weak = [], {}, None
    for day in ctx.data['days']:
        close = ctx.data['benchmark'][day][1]
        history.append(close)
        if len(history) < ma:
            continue
        avg = sum(history[-ma:])/ma
        if weak is None:
            weak = close < avg
        if close < avg*(1-band):
            weak = True
        elif close > avg*(1+band):
            weak = False
        result[day] = weak
    return result


# Integrate the Student-t survival function on [|t|, infinity) by the substitution
# x=|t|+u/(1-u). Welch has thousands of degrees of freedom on this tape.
_points, _weights = np.polynomial.legendre.leggauss(96)
_u = (_points+1)/2


def t_two_sided(t, df):
    x = abs(t)+_u/(1-_u)
    norm = math.lgamma((df+1)/2)-math.lgamma(df/2)-.5*math.log(df*math.pi)
    return min(1., float(np.dot(_weights, np.exp(norm-(df+1)/2*np.log1p(x*x/df))/(1-_u)**2)))


def welch(a, b_):
    va, vb = variance(a)/len(a), variance(b_)/len(b_)
    t = (fmean(a)-fmean(b_))/math.sqrt(va+vb)
    df = (va+vb)**2/(va**2/(len(a)-1)+vb**2/(len(b_)-1))
    return t, df, t_two_sided(t, df)


def hac_t(tape, lag):
    # Keep gaps in the calendar. Missing weeks contribute zero centered influence.
    vals = [v for v in tape if v is not None]
    avg = fmean(vals)
    z = np.array([v-avg if v is not None else 0 for v in tape])
    s = np.dot(z,z)
    for j in range(1,lag+1):
        s += 2*(1-j/(lag+1))*np.dot(z[j:],z[:-j])
    return avg*len(vals)/math.sqrt(s*len(vals)/(len(vals)-1))


def signal_test(ctx):
    days, states = ctx.data['days'], ctx.scan['states']
    rows, coverage = [], {}
    for w,(lo,hi) in WINDOWS.items():
        for h in (5,10,20):
            by_week, ic_tape, spread_tape, gate_tape, win_tape = [], [], [], [], []
            groups = [[], []]
            exclusions = Counter()
            for i,day in enumerate(days):
                if day not in ctx.data['week_ends'] or not lo <= day <= hi:
                    continue
                exclusions['weekly_signals'] += 1
                end = i+1+h
                if end >= len(days) or days[end] > hi:
                    exclusions['boundary_weeks'] += 1
                    continue
                scores = {c:s['score'] for c,s in states[i].items() if s['score'] is not None}
                ranked = sorted(scores, key=lambda c:(scores[c],c))
                buckets = [ranked[j*len(ranked)//5:(j+1)*len(ranked)//5] for j in range(5)]
                obs, ret = [[],[]], {}
                for c,s in states[i].items():
                    if not (s['warm'] and s['opened']):
                        continue
                    exclusions['warm_open_pairs'] += 1
                    entry = ctx.data['stocks'][c][days[i+1]]
                    exit_quote = ctx.data['stocks'][c][days[end]]
                    if not engine.fillable(entry, False):
                        exclusions['entry_blocked_pairs'] += 1
                        continue
                    if not exit_quote['opened']:
                        exclusions['exit_suspended_pairs'] += 1
                        continue
                    r = exit_quote['close']*states[end][c]['factor']/(entry['open']*states[i+1][c]['factor'])-1
                    if not (math.isfinite(r)):
                        raise RuntimeError('signal_test failed: math.isfinite(r)')
                    ret[c] = r
                    obs[int(s['gate'])].append(r)
                for a, vals in zip(groups, obs, strict=True):
                    a.extend(vals)
                common = [c for c in ranked if c in ret]
                ic = None
                if len(common)>=2 and len({scores[c] for c in common})>1 and len({ret[c] for c in common})>1:
                    x,y = (b.factors.ranks({c: table[c] for c in common}) for table in (scores,ret))
                    ic = correlation([x[c] for c in common],[y[c] for c in common])
                ic_tape.append(ic)
                row = dict(kind='week', window=w, horizon=h, signal_date=day, entry_date=days[i+1],
                           exit_date=days[end], ranked_at_signal=len(ranked), ranked_with_label=len(common),
                           gate_true_n=len(obs[1]), gate_false_n=len(obs[0]))
                if ic is not None:
                    row['ic'] = ic
                bins = [[ret[c] for c in names if c in ret] for names in buckets]
                spread = None
                if all(bins):
                    for j,values in enumerate(bins,1):
                        row[f'q{j}_pct'] = 100*fmean(values)
                    spread = row['q5_pct']-row['q1_pct']
                    row['q5_q1_pp'] = spread
                spread_tape.append(spread)
                dif = win = None
                if all(obs):
                    dif = 100*(fmean(obs[1])-fmean(obs[0]))
                    win = 100*(fmean(x>0 for x in obs[1])-fmean(x>0 for x in obs[0]))
                gate_tape.append(dif)
                win_tape.append(win)
                by_week.append(row)
            a, z = groups[1], groups[0]
            ic = [v for v in ic_tape if v is not None]
            quint = [r for r in by_week if 'q5_q1_pp' in r]
            lag = math.ceil((h+1)/5)-1
            summary = dict(kind='summary', window=w, horizon=h, weeks=len(by_week), ic_weeks=len(ic),
                           quintile_weeks=len(quint), ic=fmean(ic), icir=fmean(ic)/stdev(ic),
                           positive_ic_pct=100*fmean(v>0 for v in ic), ic_t_iid=fmean(ic)*math.sqrt(len(ic))/stdev(ic),
                           hac_lags=lag, ic_t_hac=hac_t(ic_tape,lag), gate_true_n=len(a), gate_false_n=len(z),
                           paired_gate_weeks=sum(x is not None for x in gate_tape), **exclusions)
            q = [fmean(r[f'q{j}_pct'] for r in quint) for j in range(1,6)]
            summary.update({f'q{j}_pct':v for j,v in enumerate(q,1)})
            summary.update(q5_q1_pp=q[-1]-q[0], monotone_increasing=all(a<=b_ for a,b_ in zip(q,q[1:])),
                           spread_t_hac=hac_t(spread_tape,lag), gate_week_diff_t_hac=hac_t(gate_tape,lag),
                           win_week_diff_t_hac=hac_t(win_tape,lag))
            for label, fn in [('return',lambda v:v), ('win',lambda v:int(v>0))]:
                v1,v0 = [fn(x) for x in a],[fn(x) for x in z]
                t,df,p = welch(v1,v0)
                summary.update({f'gate_true_{label}_pct':100*fmean(v1), f'gate_false_{label}_pct':100*fmean(v0),
                                f'gate_{label}_diff_pp':100*(fmean(v1)-fmean(v0)),
                                f'welch_{label}_t':t, f'welch_{label}_df':df, f'welch_{label}_p':p})
            if not (len(a)+len(z)+exclusions['entry_blocked_pairs']+exclusions['exit_suspended_pairs']==exclusions['warm_open_pairs']):
                raise RuntimeError("signal_test failed: verification of len(a)+len(z)+exclusions['entry_blocked_pairs']+exclusions['exit_suspe…")
            if not (len(by_week)+exclusions['boundary_weeks']==exclusions['weekly_signals']):
                raise RuntimeError("signal_test failed: len(by_week)+exclusions['boundary_weeks']==exclusions['weekly_signals']")
            coverage[f'{w}/{h}'] = dict(exclusions, labeled=len(a)+len(z), ic_weeks=len(ic), quintile_weeks=len(quint))
            rows.extend([summary, *by_week])
    return rows, coverage


class MatchedSelector:
    """Assign random names to the reference weekly slots, preserving slot continuity."""
    def __init__(self, template):
        self.template = template
        self.mapping = {}
        self.shortfalls = self.retention_breaks = 0

    def select(self, order, held, protected, macro, slots, keep_rank, targets, reject, counts):
        refs = self.template[self.day]
        if not refs:
            self.mapping = {}
            return {}
        reverse = {ref:c for c,ref in self.mapping.items()}
        # First try the previous owner of each recurring slot, with fresh names as
        # fallback. A departed slot cannot masquerade as a retained one.
        newcomers = [c for c in order if c not in self.mapping and c not in held]
        choices = {r: ([reverse[r]] if r in reverse and reverse[r] in order else []) + newcomers for r in refs}
        planned = {}
        def assign(pos, taken, buckets):
            if pos == len(refs):
                return True
            r = refs[pos]
            for c in choices[r]:
                if c in taken or macro[c] in buckets:
                    continue
                target = targets(c)
                if target is None:
                    continue
                planned[r] = c,target
                if assign(pos+1, taken|{c}, buckets|{macro[c]}):
                    return True
                del planned[r]
            return False
        # Reserve continuity before filling new slots, then restore reference execution priority.
        original = list(refs)
        refs = sorted(original, key=lambda r:r not in reverse)
        if not assign(0,set(),set()):
            for r in refs:
                for c in choices[r]:
                    if any(macro[other]==macro[c] for other,t in planned.values()):
                        continue
                    if (target:=targets(c)) is not None:
                        planned[r] = c,target
                        break
        self.shortfalls += len(original)-len(planned)
        self.retention_breaks += sum(r in reverse and (r not in planned or planned[r][0]!=reverse[r]) for r in original)
        self.mapping = {planned[r][0]:r for r in original if r in planned}
        return {c:t for c,t in (planned[r] for r in original if r in planned)}

    def matched_stop(self, day, code, states):
        ref = self.mapping.get(code)
        return ref is not None and native_stop(day, ref, states)


def stats_for_book(book):
    path = book['path']
    summary = compute_portfolio_metrics(path, book['orders'], CAP)
    return dict(**{k: summary[k] for k in ('net_pct', 'cagr_pct', 'max_dd_pct', 'sharpe', 'calmar')}, trades=len(book['orders']),
                trend_trades=sum(f['sleeve']==b.TREND for f in book['orders']),
                mean_trend_names=fmean(len(r['sleeves'][b.TREND]) for r in path),
                oneway_turnover_pct=summary['period_oneway_turnover_pct'])


def distribution(raw, truth, fields, **key):
    result = []
    if not (len(raw)==N and len({r['seed'] for r in raw})==N):
        raise RuntimeError("distribution failed: len(raw)==N and len({r['seed'] for r in raw})==N")
    for m in fields:
        vals = [r[m] for r in raw]
        real = truth[m]
        result.append(dict(kind='distribution', **key, metric=m, real=real, n=N, mean=fmean(vals),
                           p50=diag.quantile(vals,50), p90=diag.quantile(vals,90), p95=diag.quantile(vals,95),
                           percentile=100*(sum(v<real for v in vals)+.5*sum(v==real for v in vals))/N,
                           p_upper=(1+sum(v>=real for v in vals))/(N+1)))
    return result


def stock_tests(ctx, all_order, vols, timing, real):
    rows = []
    broad = replace(ctx,vol=vols)
    bottom_gate = {i: sorted(ctx.scan['ranks'][i], key=lambda c:(ctx.scan['states'][i][c]['score'],c)) for i in all_order}
    for wi,w in enumerate(SPLIT):
        for model in MODELS:
            key = dict(window=w, model=model)
            clock = timing if model==M200 else None
            actual = real[w,model]
            truth = stats_for_book(actual)
            rows.append(dict(kind='control', selection='real', **key, **truth))
            for name,avail,stop in [('bottom_gate',bottom_gate,native_stop), ('bottom_all',all_order,lambda *args:False)]:
                book = run(broad,w,timing=clock,order=avail,stop=stop)
                rows.append(dict(kind='control', selection=name, **key, **stats_for_book(book)))
            targets = {p['date']:list(p['targets'][b.TREND]) for p in actual['plans']}
            idx = [(ctx.day_index[d], d) for d in targets]
            for selection in ('random_all_matched', 'random_gate_matched'):
                raw = []
                for rep in range(N):
                    seed = STOCK_SEED+wi*1000+rep
                    rng = random.Random(seed)
                    order = {}
                    for i,_ in idx:
                        names = sorted(all_order[i] if selection=='random_all_matched' else ctx.scan['ranks'][i])
                        rng.shuffle(names)
                        order[i] = names
                    selector = MatchedSelector(targets)
                    stop = selector.matched_stop if selection=='random_all_matched' else native_stop
                    book = run(broad,w,timing=clock,selector=selector,order=order,stop=stop)
                    raw.append(dict(kind='draw', selection=selection, **key, draw=rep+1, seed=seed,
                                    target_slots=sum(map(len,targets.values())), target_shortfalls=selector.shortfalls,
                                    forced_remaps=selector.retention_breaks, **stats_for_book(book)))
                rows.extend(raw+distribution(raw, truth, SM, selection=selection, **key))
            print(f'Stock randomization complete: {w} {model}', flush=True)
    return rows


def permute_blocks(flags, rng):
    runs = [(flag,len(list(group))) for flag,group in itertools.groupby(flags)]
    lengths = {f:[n for flag,n in runs if flag==f] for f in (False,True)}
    for vals in lengths.values():
        rng.shuffle(vals)
    out = [flag for flag,n in runs for _ in range(lengths[flag].pop())]
    if not (sorted((f,len(list(g))) for f,g in itertools.groupby(out)) == sorted(runs)):
        raise RuntimeError('permute_blocks failed: sorted((f,len(list(g))) for f,g in itertools.groupby(out)) == sorted(runs)')
    return out


def timing_tests(ctx, timing, real):
    rows, plateau = [], []
    for wi,w in enumerate(SPLIT):
        old = real[w,M200]
        actual = stats_for_book(old)
        dates = [r['date'] for r in old['regimes'] if r['date']>=WINDOWS[w][0]]
        flags = [timing[d] for d in dates]
        count = dict(weeks=len(flags), weak_weeks=sum(flags), flips=diag.switches(flags))
        rows.append(dict(kind='control', selection='real', window=w, **count, **actual))
        inv = run(ctx,w,timing={d:not f for d,f in timing.items()})
        rows.append(dict(kind='control', selection='inverse', window=w, weeks=len(flags),weak_weeks=len(flags)-sum(flags),
                         flips=count['flips'], **stats_for_book(inv)))
        raw = []
        for rep in range(N):
            seed = TIMING_SEED+wi*1000+rep
            shuffled = permute_blocks(flags,random.Random(seed))
            clock = timing | dict(zip(dates,shuffled,strict=True))
            book = run(ctx,w,timing=clock)
            observed = [r['weak'] for r in book['regimes'] if r['date']>=WINDOWS[w][0]]
            if not (observed==shuffled and sum(observed)==count['weak_weeks'] and diag.switches(observed)==count['flips']):
                raise RuntimeError("timing_tests failed: observed==shuffled and sum(observed)==count['weak_weeks'] and diag.switches(observed)==count['flips']")
            raw.append(dict(kind='draw', selection='block', window=w, draw=rep+1, seed=seed, **count,
                            schedule_sha256=hashlib.sha256(bytes(shuffled)).hexdigest(), **stats_for_book(book)))
        rows.extend(raw+distribution(raw,actual,TM,selection='block',window=w))
        axes = [('ma',[(m,2,2) for m in (160,180,200,220,240)]),
                ('band',[(200,x,2) for x in range(5)]), ('kt',[(200,2,k) for k in (2,3,4)])]
        for axis, cells in axes:
            for ma,band,k in cells:
                clock = hysteresis(ctx,ma,band/100)
                book = run(ctx,w,timing=clock,kt=k)
                m = metrics(book['path'])
                if (ma,band,k)==(200,2,2):
                    if not (all(m[key]==actual[key] for key in m)):
                        raise RuntimeError('timing_tests failed: all(m[key]==actual[key] for key in m)')
                plateau.append(dict(window=w,axis=axis,ma=ma,band_pct=band,kt=k,keep_rank=k+2,**m))
        print(f'Timing randomization complete: {w}', flush=True)
    return rows, plateau


def leave_out(ctx, facts, rankings, timing):
    rows, equal_by_pool = [], {}
    for (w,model), stocks in rankings.items():
        ordered = sorted(stocks,key=lambda c:(-stocks[c][0],c))
        net = sum(v[0] for v in stocks.values())
        winners = sum(max(0,v[0]) for v in stocks.values())
        for n,c in enumerate(ordered[:10],1):
            pnl, realized, unrealized = stocks[c]
            rows.append(dict(kind='top10',window=w,model=model,rank=n,code=c,name=ctx.data['universe'][c]['name'],
                             net_pnl_rmb=pnl/100,realized_rmb=realized/100,unrealized_rmb=unrealized/100,
                             trend_net_rmb=net/100,trend_gross_winners_rmb=winners/100,
                             share_of_net_pct=100*pnl/net,share_of_gross_winners_pct=100*pnl/winners))
        for k in (3,5):
            removed = ordered[:k]
            d = shared.pool_data(ctx.data,removed)
            keep = d['universe'].keys()
            states = [{c:s for c,s in day.items() if c in keep} for day in ctx.scan['states']]
            ranks = [[c for c in day if c in keep] for day in ctx.scan['ranks']]
            scan = dict(states=states,ranks=ranks)
            sub = replace(ctx,data=d,scan=scan,defense=b.defensive_features(d,scan,facts),
                          macro={c:ctx.macro[c] for c in keep})
            if not (len(keep)==len(ctx.data['universe'])-k):
                raise RuntimeError("leave_out failed: len(keep)==len(ctx.data['universe'])-k")
            strategy = run(sub,w,timing=timing if model==M200 else None)
            pool_id = w, tuple(sorted(keep))
            if pool_id not in equal_by_pool:
                equal = benchmark(sub,w,'Equal')
                if not (all(f['code'] in keep for f in equal['orders'])):
                    raise RuntimeError("leave_out failed: all(f['code'] in keep for f in equal['orders'])")
                if not (all(set(r['hold'])<=keep for r in equal['path'])):
                    raise RuntimeError("leave_out failed: all(set(r['hold'])<=keep for r in equal['path'])")
                equal_by_pool[pool_id] = metrics(equal['path'])
            if not (all(f['code'] in keep for f in strategy['orders'])):
                raise RuntimeError("leave_out failed: all(f['code'] in keep for f in strategy['orders'])")
            if not (all(set(r['hold'])<=keep for r in strategy['path'])):
                raise RuntimeError("leave_out failed: all(set(r['hold'])<=keep for r in strategy['path'])")
            m, ref = metrics(strategy['path']), equal_by_pool[pool_id]
            rows.append(dict(kind='exclusion',window=w,model=model,k=k,remaining=len(keep),
                             removed='|'.join(removed),removed_net_share_pct=100*sum(stocks[c][0] for c in removed)/net,
                             **m, **{'equal_'+key:value for key,value in ref.items()},
                             excess_cagr_pp=m['cagr_pct']-ref['cagr_pct'],beats_equal=m['net_pct']>ref['net_pct']))
    return rows


def initial_runs(ctx,timing):
    records = {(r['window'],r['variant']):r for r in diag.read_csv(p2.OUT/'p2_ablation_all_runs.csv')
               if r['pool']=='50' and r['scenario']=='2bp'}
    equal_record = diag.read_json(ROOT/'results/research/pool50_barbell.json')['pools']['50 股']['results']['月度等权']
    rows, real, rankings, checks = [], {}, {}, {}
    for w in WINDOWS:
        for model in ('CSI300','Equal50','Defense50_Cash50','Trend50_Cash50',*MODELS):
            daily = None
            if model in ('CSI300','Equal50'):
                book = benchmark(ctx,w,'CSI300' if model=='CSI300' else 'Equal')
                if model=='Equal50':
                    record = equal_record[w]['2bp']
                    if not (len(book['orders']) == record['trades']):
                        raise RuntimeError("initial_runs failed: len(book['orders']) == record['trades']")
                    if not (abs(metrics(book['path'])['cagr_pct']-record['cagr']*100)<1e-9):
                        raise RuntimeError("initial_runs failed: abs(metrics(book['path'])['cagr_pct']-record['cagr']*100)<1e-9")
            else:
                sleeve = {'Defense50_Cash50':'defense','Trend50_Cash50':'trend'}.get(model,'both')
                book = run(ctx,w,timing=timing if model==M200 else None,sleeves=sleeve)
                daily, stocks = pnl_ledger(ctx,book)
                if model in MODELS:
                    check_archive(book,w,model,records[w,model])
                    checks[f'{w}/{model}'] = dict(days=len(daily),fills=len(book['orders']))
                    if w in SPLIT:
                        real[w,model] = book
                    if w in ('W1',*SPLIT):
                        rankings[w,model] = stocks
                else:
                    if not (all(f['sleeve']==(b.VALUE if sleeve=='defense' else b.TREND) for f in book['orders'])):
                        raise RuntimeError("initial_runs failed: all(f['sleeve']==(b.VALUE if sleeve=='defense' else b.TREND) for f in book['orders'])")
            expected = [d for d in ctx.data['days'] if WINDOWS[w][0]<=d<=WINDOWS[w][1]]
            if not ([r['date'] for r in book['path']] == expected):
                raise RuntimeError("initial_runs failed: [r['date'] for r in book['path']] == expected")
            rows.extend(attr_rows(w,model,book,daily))
    equal = {r['period']:r['net_pct'] for r in rows if r['model']=='Equal50'}
    for r in rows:
        r['excess_net_pp'] = r['net_pct']-equal[r['period']]
        r['beats_equal'] = r['excess_net_pp']>0
    return rows, real, rankings, checks


def main():
    require_runtime_guard()
    if not (abs(t_two_sided(1,1)-.5)<1e-12 and abs(t_two_sided(2.2281388519649385,10)-.05)<1e-10):
        raise RuntimeError('main failed: abs(t_two_sided(1,1)-.5)<1e-12 and abs(t_two_sided(2.2281388519649385,10)-.05)<1e-10')
    before = snapshot()
    frozen = verify_frozen_locks(ROOT)
    verify_required_sha_manifest(before, frozen, CORE, 'model audit frozen engines')
    if not (shared.CASES['2bp']==(0.,0,2)):
        raise RuntimeError("main failed: shared.CASES['2bp']==(0.,0,2)")
    if not (b.local_identity()==diag.read_json(ROOT/'results/research/pool50_barbell.json')['identity']):
        raise RuntimeError("main failed: b.local_identity()==diag.read_json(ROOT/'results/research/pool50_barbell.json')['identity']")
    data,prefix,macro = shared.load_tape()
    del prefix
    audit = diag.prepare_receipt(data,diag.read_json(ROOT/'results/data_audit.json'))
    if not (audit['stock_issues']==audit['etf_issues']==audit['index_missing']==0):
        raise RuntimeError("main failed: audit['stock_issues']==audit['etf_issues']==audit['index_missing']==0")
    if not (all(r['missing']==0 and r['rows']==r['expected'] for r in data['coverage'])):
        raise RuntimeError("main failed: all(r['missing']==0 and r['rows']==r['expected'] for r in data['coverage'])")
    scan = engine.scan(data)
    vol = shared.vol20(data,scan)
    facts = b.factors.read_observations(b.DATA/'daily_basic.csv')
    days = data['days']
    ctx = Context(data,scan,vol,b.defensive_features(data,scan,facts),b.month_closes(data['week_ends'],days[-1],days[-1]),
                  macro,{d:i for i,d in enumerate(days)})
    timing = hysteresis(ctx,200,.02)
    official = diag.index_tape(data)
    if not (all(official[d][diag.state_key(200,'hysteresis')]==state for d,state in timing.items())):
        raise RuntimeError("main failed: all(official[d][diag.state_key(200,'hysteresis')]==state for d,state in timing.items())")
    OUT.mkdir(parents=True,exist_ok=True)
    attr, real, rankings, checks = initial_runs(ctx,timing)
    write_csv('attribution_yearly',attr)
    print('Eight archived ledgers and four equal-weight windows verified.',flush=True)
    signals, sig_counts = signal_test(ctx)
    write_csv('signal_ic_quintile',signals)
    leave = leave_out(ctx,facts,rankings,timing)
    write_csv('leave_top_k_out',leave)
    all_order, all_vol = market_states(ctx)
    stock = stock_tests(ctx,all_order,all_vol,timing,real)
    write_csv('placebo_stock_mc',stock)
    clocks, plateau = timing_tests(ctx,timing,real)
    write_csv('placebo_timing_mc',clocks)
    write_csv('param_plateau',plateau)
    sizes = dict(attribution_yearly=len(attr), signal_ic_quintile=len(signals), leave_top_k_out=len(leave),
                 placebo_stock_mc=len(stock), placebo_timing_mc=len(clocks), param_plateau=len(plateau))
    if not (len(attr)==6*(4+11) and len(leave)==6*(10+2) and len(plateau)==2*(5+5+3)):
        raise RuntimeError('main failed: len(attr)==6*(4+11) and len(leave)==6*(10+2) and len(plateau)==2*(5+5+3)')
    if not (sum(r['kind']=='draw' for r in stock)==N*2*2*2 and sum(r['kind']=='draw' for r in clocks)==N*2):
        raise RuntimeError("main failed: sum(r['kind']=='draw' for r in stock)==N*2*2*2 and sum(r['kind']=='draw' for r in clocks)==N*2")
    required = {
        'attribution_yearly': {'all': (*SM,'max_dd_pct','net_pct','closing_nav')},
        'signal_ic_quintile': {'summary': ('ic','icir','ic_t_hac','ic_t_iid','positive_ic_pct','q5_q1_pp',
                                          'q1_pct','q2_pct','q3_pct','q4_pct','q5_pct','welch_return_p','welch_win_p'),
                               'week': ('ranked_at_signal','ranked_with_label','gate_true_n','gate_false_n')},
        'leave_top_k_out': {'top10': ('rank','net_pnl_rmb','realized_rmb','unrealized_rmb','share_of_net_pct'),
                           'exclusion': (*SM,'equal_cagr_pct','excess_cagr_pp')},
        'param_plateau': {'all': (*SM,'max_dd_pct')},
    }
    for name in ('placebo_stock_mc','placebo_timing_mc'):
        required[name] = {'control': (*SM,'max_dd_pct'), 'draw': (*SM,'max_dd_pct','seed'),
                          'distribution': ('real','mean','p50','p90','p95','percentile','p_upper')}
    for name,n in sizes.items():
        published = diag.read_csv(OUT/(name+'.csv'))
        if not (len(published)==n):
            raise RuntimeError('main failed: len(published)==n')
        if not (all(math.isfinite(float(row[k])) for row in published
                   for k in required[name][row.get('kind','all')])):
            raise RuntimeError(name)
    after = snapshot()
    if not (before==after):
        raise RuntimeError('main failed: before==after')
    receipt = dict(status='PASS',python=sys.version, numpy=np.__version__,script_sha256=digest(Path(__file__)),
                   sha256_before={f:before[f] for f in CORE},sha256_after={f:after[f] for f in CORE},
                   cache_dir_skipped_for_routine_run=True,protected_input_sha256=before,verified_input_files=len(before),windows=WINDOWS,capital_rmb=CAP/100,
                   scenario_2bp=shared.CASES['2bp'], seeds=dict(stock_base=STOCK_SEED,timing_base=TIMING_SEED,
                   formula='base + 1000 * window_index(IS=0,OOS=1) + replicate_index(0..199)',draws=N),
                   trading_days=len(days),first=days[0],last=days[-1],universe=len(data['universe']),
                   data_audit=audit,stock_coverage=[{k:r[k] for k in ('code','rows','expected','open','suspended','missing')}
                                                   for r in data['coverage']],archive_reconciliation=checks,
                   signal_samples=sig_counts,csv_rows=sizes,assertions=dict(missing_postlisting_rows=0,
                   missing_index_rows=0,missing_daily_nav_rows=0,unreconciled_pnl_cents=0,missing_mc_draws=0,
                   missing_required_metrics=0,unclassified_forward_pairs=0,block_schedule_count_mismatches=0),
                   output_sha256={f+'.csv':digest(OUT/(f+'.csv')) for f in sizes})
    report = render(attr,signals,stock,leave,clocks,plateau,receipt)
    atomic_text(OUT/'model_reality_report.md',report)
    receipt['output_sha256']['model_reality_report.md'] = digest(OUT/'model_reality_report.md')
    atomic_text(OUT/'audit_manifest.json',json.dumps(receipt,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    print('Completed all five tests and verified requested outputs.',flush=True)


def render(attr,signals,stock,leave,clocks,plateau,receipt):
    table = b.md_table
    def num(v):
        return f'{v:.2f}'
    def pv(v):
        return f'{v:.4f}'
    def mc_cell(r):
        return f"{r['real']:.3f} / P{r['percentile']:.1f} / {pv(r['p_upper'])}"
    a = {(r['period'],r['model']):r for r in attr}
    years = [str(y) for y in range(2016,2027)]
    names = ('CSI300','Equal50','Defense50_Cash50','Trend50_Cash50',*MODELS)
    labels = dict(zip(names,('沪深300','月度等权','防御50%＋现金','趋势50%＋现金','BASE','M200')))
    wins = {m:sum(a[y,m]['beats_equal'] for y in years) for m in names}
    strength = all(a[w,m]['beats_equal'] for w in WINDOWS for m in MODELS)
    g1 = '通过' if strength and all(wins[m]>=6 for m in MODELS) else '存疑'
    sig = [r for r in signals if r['kind']=='summary' and r['window'] in SPLIT]
    g2 = '通过' if all(sum(r['ic_t_hac']>=1.96 and r['q5_q1_pp']>0 for r in sig if r['window']==w)>=2
                       for w in SPLIT) else '存疑'
    stock_d = {(r['window'],r['model'],r['selection'],r['metric']):r for r in stock if r['kind']=='distribution'}
    g3 = '通过' if all(stock_d[w,m,s,'cagr_pct']['p_upper']<=.05 for w in SPLIT for m in MODELS
                       for s in ('random_gate_matched','random_all_matched')) else '存疑'
    ex = [r for r in leave if r['kind']=='exclusion']
    pass_count = sum(r['beats_equal'] for r in ex)
    # This is a directly observable robustness claim about all requested windows.
    g4 = '通过' if pass_count==len(ex) else '证伪'
    timing_d = {(r['window'],r['metric']):r for r in clocks if r['kind']=='distribution'}
    g5 = '通过' if all(timing_d[w,m]['p_upper']<=.05 for w in SPLIT for m in TM) else '存疑'
    is_oos = [r for r in sig if r['window']=='OOS']
    lookup = {(r['window'],r['model'],r['selection']):r for r in stock if r['kind']=='control'}
    top = {(r['window'],r['model'],r['rank']):r for r in leave if r['kind']=='top10'}
    text = ['# 50 股哑铃策略真实能力证伪审计',
            f"在这组事后选出的龙头股中，月度等权的 W1 年化已经达到 **{a['W1','Equal50']['cagr_pct']:.2f}%**。"
            f"BASE 为 **{a['W1','BASE']['cagr_pct']:.2f}%**，M200 为 **{a['W1',M200]['cagr_pct']:.2f}%**。"
            f"两者分别有 **{wins['BASE']}/11**、**{wins[M200]}/11** 个年份战胜等权。"
            f"从收益直接推论普遍的趋势预测力，证据不足。OOS 三个持有期的平均 RankIC 为 "
            + '、'.join(f"{r['ic']:+.3f}" for r in is_oos)+'。',
            table(['检验命题','评级','判断范围'],[
                ['完整组合相对同池等权有历史收益增量',g1,'四个重叠窗口和 11 段日历年'],
                ['冻结评分具有可重复的截面排序能力',g2,'原生有效分数、周截面、IS 与 OOS'],
                ['Top-2 的收益超过同结构随机选股',g3,'两种随机池、两种预算、每格 200 次'],
                ['剔除头部赢家后各窗口都能跑赢剩余池等权',g4,f'{pass_count}/{len(ex)} 组跑赢'],
                ['真实弱势周的位置具有独立择时价值',g5,'强弱段重排、反向择时与指定邻域']]),
            'W1 为 2016-01-04 至 2026-11-20，IS 截至 2023-01-20，OOS 自次日起至冻结日。'
            'W_Bear 为 2021-03-01 至 2023-11-19。各窗以 45 万元现金独立运行。W_Bear 横跨切点，'
            'W1 包含其余窗口。OOS 已被前期研究看过，属于历史留出复核。固定池以外的收益、退市样本、实时预测和实际成交均没有在本次实验证实。',
            '## 1. 收益分解 · '+g1,
            table(['窗口','组合','CAGR %','最大回撤 %','夏普','卡玛','趋势贡献 pp','防御贡献 pp'],
                  ([w,labels[m],*(num(r[k]) for k in ('cagr_pct','max_dd_pct','sharpe','calmar')),
                    *(num(r[k]) if k in r else '—' for k in ('trend_cagr_pp','defense_cagr_pp'))]
                   for w in WINDOWS for m in names for r in [a[w,m]])),
            '逐年使用 W1 的连续持仓，以前一年末净值为期初。下表列实际区间收益，2026 年截至 11 月 20 日。'
            '六条线逐年和分窗的四项指标及袖子贡献均见 attribution_yearly.csv。',
            table(['年度／胜年数',*(labels[m] for m in names)],[
                *([y,*(num(a[y,m]['net_pct'])+'%' for m in names)] for y in years),
                ['胜等权',*(f'{wins[m]}/11' if m!='Equal50' else '基准' for m in names)]]),
            '防御与趋势单袖子线分别关闭另一袖子的下单，保留 50% 预算上限及零息现金。完整组合共用现金，'
            '防御预留、持仓互斥、整手门槛和再平衡随组合净值变化。归因按每个袖子的成交现金流、应计股利和收盘持仓逐日对账到分。'
            '将各自期间净盈亏占总净盈亏的比例乘组合同期 CAGR，得到可加总的年化贡献。独立单袖子回报另列。',
            'CAGR 使用首末实际交易日之间的日历天数与 365.25 天。最大回撤含期初高水位，夏普用零息日收益的样本标准差及 √252。'
            '股票账户均使用原撮合的佣金、印花税、开盘 2bp、停牌与限价规则。沪深300使用冻结引擎的指数点位，自首日开盘持有，无交易费用。',
            '## 2. 原生预测力 · '+g2,
            table(['样本','H','IC周数','RankIC','ICIR','正IC %','常规 t','HAC t','Q1 %','Q2 %','Q3 %','Q4 %','Q5 %','Q5−Q1 pp','单调'],
                  ([r['window'],r['horizon'],r['ic_weeks'],f"{r['ic']:.4f}",f"{r['icir']:.3f}",
                    *(num(r[k]) for k in ('positive_ic_pct','ic_t_iid','ic_t_hac',*(f'q{i}_pct' for i in range(1,6)),'q5_q1_pp')),
                    '是' if r['monotone_increasing'] else '否'] for r in sig)),
            table(['样本','H','过门／未过门 N','均值差 pp','Welch t / p','胜率差 pp','Welch t / p','按周配对 HAC t（收益／胜率）'],
                  ([r['window'],r['horizon'],f"{r['gate_true_n']} / {r['gate_false_n']}",num(r['gate_return_diff_pp']),
                    f"{r['welch_return_t']:.2f} / {pv(r['welch_return_p'])}",num(r['gate_win_diff_pp']),
                    f"{r['welch_win_t']:.2f} / {pv(r['welch_win_p'])}",
                    f"{r['gate_week_diff_t_hac']:.2f} / {r['win_week_diff_t_hac']:.2f}"] for r in sig)),
            'T 为完整自然周的最后交易日。标签取 T+1 可买开盘至 T+1+H 收盘的复权毛收益，剔除入场停牌／开盘封涨停及出口停牌。'
            '按收盘价估值；不假设收盘可成交。T 和出口都须落在同一窗口。原生 score 只存在于过门且开市的证券。'
            'RankIC 对当周至少两只有效且有变异的分数与收益取中秩相关，按周等权；ICIR 为周均值除以周样本标准差。'
            '五组在 T 时按分数升序先分组，再取有标签的组均值；五组齐全的周才进入分组汇总。CSV 同时保留周明细、边界和不可成交的计数。',
            'gate 比较限同样预热满 252 个可交易日、T 日开市且有未来标签的样本。胜率为收益严格大于零的比例。'
            'Welch 双侧检验按股票周合并，截面共振和持有期重叠会使常规标准误偏小。按周配对的差及 RankIC 另用 Bartlett HAC，'
            '5／10／20 日分别取 1／2／4 个周滞后。分组收益差和全截面排序测量不同方面；正的尾组差可以与接近零的 RankIC 同时出现。',
            '## 3. 反转和随机选股 · '+g3,
            f"OOS 中，真实 BASE 和 M200 都胜过两种反转对照。与保留相同持仓节奏的全池随机组合相比，"
            f"两者年化收益的单侧 p 分别为 {pv(stock_d['OOS','BASE','random_all_matched','cagr_pct']['p_upper'])}、"
            f"{pv(stock_d['OOS',M200,'random_all_matched','cagr_pct']['p_upper'])}。过门随机池中的对应值为 "
            f"{pv(stock_d['OOS','BASE','random_gate_matched','cagr_pct']['p_upper'])}、"
            f"{pv(stock_d['OOS',M200,'random_gate_matched','cagr_pct']['p_upper'])}。"
            '方向上有优势，OOS 年化收益尚难排除抽样运气。部分卡玛比较达到单项 5% 水平，所有检验的 p 值均未作多重比较校正。',
            table(['窗口','预算','真实 Top-2','过门 Bottom-2','全池 Bottom-2'],
                  ([w,labels[m],*(' / '.join(num(lookup[w,m,s][k]) for k in SM)
                                  for s in ('real','bottom_gate','bottom_all'))] for w in SPLIT for m in MODELS)),
            '上表依次为 CAGR %、夏普、卡玛。全池反转在当日开市且已预热的股票中，用冻结的 20／60 日回归公式补算分数，'
            '与每个过门原生分数及波动上限逐项核对；该对照同时关闭日度 gate 离场。过门反转保留原日度离场。两组均沿用 Top-2 的留仓缓冲。',
            table(['窗口','预算','随机池','CAGR（实值／分位／p）','夏普（实值／分位／p）','卡玛（实值／分位／p）'],
                  ([w,labels[m],label,*(mc_cell(stock_d[w,m,s,k]) for k in SM)] for w in SPLIT for m in MODELS
                   for s,label in [('random_all_matched','全池'),('random_gate_matched','过门')])),
            '全池随机检验只置换持仓身份。每个周收盘复制真实策略当时已知的目标席位数、留换节奏，将席位配给当日可交易、预热完成的随机股票；'
            '全池对照逐日沿用相应真实席位当日收盘的 gate 离场时点。过门对照从 gate=True 股票抽取，并跟随抽中股票自己的日度 gate。'
            '每笔成交均重新撮合，按抽中股票计算波动上限、整手、涨跌停与费用。防御按同一 50% 规则在各自账本运行。'
            '持仓互斥、跨桶或可负担性会迫使换名或留空，逐次 target_shortfalls、forced_remaps、实际持仓数及换手见 CSV。',
            '每个场景固定 200 个种子，两个预算使用相同种子。分位以严格低于加一半平局计，单侧 p=(1+不差于真实的次数)/201。'
            '逐次结果及均值、P50、P90、P95 见 placebo_stock_mc.csv；这些 p 值衡量在指定反事实中的罕见程度。',
            '## 4. 头部赢家剔除 · '+g4,
            '原持仓赚钱集中，与剔除后还能否找到替代股票，是两个不同问题。'
            + '、'.join(f"{labels[r['model']]} 的前五只贡献 {r['removed_net_share_pct']:.1f}%"
                       for r in ex if r['window']=='W1' and r['k']==5)
            + '的 W1 趋势净利。剔除各自前五只后，'
            + '，'.join(w+' 的 BASE / M200 相对等权年化差为 '
                       +' / '.join(f"{r['excess_cagr_pp']:+.2f}" for r in ex if r['window']==w and r['k']==5)+' 个百分点'
                       for w in SPLIT)
            + '。IS 给出了跨窗稳健性的反例，OOS 的替代持仓仍有超额。',
            table(['名次',*(f'{w} {labels[m]}' for w in ('W1',*SPLIT) for m in MODELS)],
                  ([i,*(f"{r['name']} {r['share_of_net_pct']:.1f}%" for w in ('W1',*SPLIT) for m in MODELS
                        for r in [top[w,m,i]])] for i in range(1,11))),
            table(['窗口','组合','剔除','剔除代码','占趋势净利 %','剩余池 CAGR %','对应等权 %','超额 pp'],
                  ([r['window'],labels[r['model']],r['k'],r['removed'].replace('|','、'),
                    *(num(r[k]) for k in ('removed_net_share_pct','cagr_pct','equal_cagr_pct','excess_cagr_pp'))] for r in ex)),
            '每个窗口、每种策略独立按该账本的趋势净盈利排序。已实现部分含手续费、印花税及已获权益的现金股利；期末持仓以收盘市值减剩余成本计未实现盈利。'
            '主分母为含所有亏损股票的趋势净利，头部比例可超过 100%；CSV 另给盈利股票正净利之和作分母。'
            '剔除后重算同池防御因子截面、趋势排序及月度等权，两个袖子和对照均无法买入剔除股。'
            '名单使用本窗口事后盈亏，衡量集中依赖，不能用作前瞻剔除规则。',
            '## 5. M200 择时与参数邻域 · '+g5,
            f"OOS 真实弱势周的夏普在置换中排 P{timing_d['OOS','sharpe']['percentile']:.1f}，卡玛排 "
            f"P{timing_d['OOS','calmar']['percentile']:.1f}。最大回撤只排 P{timing_d['OOS','max_dd_pct']['percentile']:.1f}，"
            '多数同样频率、同样持续时间的降仓安排回撤更浅。M200 的历史降仓效果不足以证明它能识别更值得降仓的周。',
            table(['样本','弱势周／总周','切换','夏普（实值／分位／p）','卡玛（实值／分位／p）','最大回撤 %（实值／分位／p）','反向夏普／卡玛／回撤 %'],
                  ([w,f"{r['weak_weeks']}/{r['weeks']}",r['flips'],*(mc_cell(timing_d[w,k]) for k in TM),
                    ' / '.join(num(inv[k]) for k in TM)] for w in SPLIT
                   for r in [next(x for x in clocks if x['kind']=='control' and x['window']==w and x['selection']=='real')]
                   for inv in [next(x for x in clocks if x['kind']=='control' and x['window']==w and x['selection']=='inverse')])),
            '2015 年起按交易日更新沪深300均线和滞回状态，周收盘决定下次执行预算。每窗将强段、弱段各自按长度随机重排并交替拼回；'
            '窗内弱周数、切换次数、各段长度分布及首末状态逐次相符。窗前用来下首单的信号固定，末周待执行计划沿用原账本。'
            '最大回撤使用带负号的收益，越接近零分位越高。反向对照将强势周趋势上限降至 25%，弱势周恢复 50%。',
            table(['单维扫描','取值顺序','IS 夏普','IS 卡玛','OOS 夏普','OOS 卡玛'],
                  ([label,values,*(' / '.join(num(r[k]) for r in plateau if r['window']==w and r['axis']==axis)
                                  for w in SPLIT for k in ('sharpe','calmar'))]
                    for axis,label,values in [('ma','均线日数','160 / 180 / 200 / 220 / 240'),('band','滞回半宽 %','0 / 1 / 2 / 3 / 4'),
                                               ('kt','趋势持仓数','2 / 3 / 4')])),
            '扫描围绕 MA200、±2%、Kt=2，每次移动一维。Kt 的支持规则沿用原 Barbell 的 Kt+2 留仓缓冲。参数只用于敏感度诊断。',
            '## 结论与证据边界']
    for axis,label in [('ma','均线'),('band','滞回带'),('kt','持仓数')]:
        deviations = []
        for w in SPLIT:
            subset = [r for r in plateau if r['window']==w and r['axis']==axis]
            center = next(r for r in subset if (r['ma'],r['band_pct'],r['kt'])==(200,2,2))
            deviations.append(max(abs(r[k]/center[k]-1) for r in subset for k in ('sharpe','calmar')))
        text.append(f"{label}邻域的夏普或卡玛相对中心的最大偏离，IS 为 {100*deviations[0]:.1f}%，OOS 为 {100*deviations[1]:.1f}%。"
                    + ('在这两个窗口内变化较小。' if max(deviations)<=.20 else '至少一窗偏离超过 20%，该维度不能称为平滑高原。'))
    tail = [r for r in ex if r['window']=='OOS' and r['k']==5]
    text.append(f"两组 OOS 剔除前五后，BASE 和 M200 相对各自等权的年化差为 {tail[0]['excess_cagr_pp']:+.2f}、"
                f"{tail[1]['excess_cagr_pp']:+.2f} 个百分点。真实 M200 在 OOS 的夏普和卡玛在同段长安慰剂中的分位分别为 "
                f"P{timing_d['OOS','sharpe']['percentile']:.1f}、P{timing_d['OOS','calmar']['percentile']:.1f}。"
                '资产池的事后选择偏差贯穿全部实验；要识别池本身贡献，还缺逐期可投资、包含退出证券的历史成分对照。'
                '这次回放能定位固定池内的额外收益及其依赖，无法量化整个股票池有多少收益源自事后选样。')
    text.extend(['## 数据与复现',
                 '本次独立检查股票上市后日线和沪深300完整性，核对 8 本归档账户的逐日净值、持仓及逐笔成交。'
                 f"输入覆盖 {receipt['universe']} 股、{receipt['trading_days']} 个交易日（{receipt['first']} 至 {receipt['last']}）；"
                 'stock_issues=0，etf_issues=0。本次运行研究账本，production_replay 为未运行；已有正式验收记录为 true。'
                 'ETF 份额独立于本次股票实验。CI、双端合成测试及实盘交易本次均未运行。',
                 table(['ETF','首末观测','观测／冻结日可用行','最后可用观测日','末条可用时点'],
                       ([r['code'],f"{r['first']} 至 {r['last']}",f"{r['rows']} / {r['usable']}",
                         r['last_usable_observation'],r['last_observation_available_on']] for r in receipt['data_audit']['prepared_etf_coverage'])),
                 '在仓库根目录用 Python 3.11（含 NumPy）运行 `python -B -X utf8 scripts/audit_model_reality.py`。'
                 'audit_manifest.json 记录前后哈希、抽样种子、样本剔除、逐日对账及输出摘要。CSV 的空格表示该行类型不适用，'
                 '周截面不足或无变化时的统计量留空。零缺失断言适用于必需输入和输出；显式停牌、ETF 覆盖缺口、结构性无分数均分别计数。'])
    return '\n\n'.join(text)+'\n'


if __name__=='__main__':
    main()
