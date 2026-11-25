"""Weekly cross-sectional inference. Group membership is fixed before observing labels."""
import numpy as np


def midranks(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return np.full(len(x), .5)
    order = np.argsort(x, kind="stable")
    edges = np.r_[0, np.flatnonzero(np.diff(x[order])) + 1, len(x)]
    ranks = np.empty(len(x))
    ranks[order] = np.repeat((edges[:-1]+edges[1:]-1)/(2*(len(x)-1)), np.diff(edges))
    return ranks


def value_scores(pe, dividend):
    ep = np.divide(1., pe, out=np.zeros(len(pe)), where=pe > 0)
    ep_rank = np.where(ep > 0, midranks(ep), 0.)
    dv_rank = midranks(np.maximum(0., np.nan_to_num(dividend, nan=0.)))
    return ep_rank, dv_rank, (ep_rank + dv_rank) / 2


def mean(x):
    vals = np.asarray(x, dtype=float)
    vals = vals[np.isfinite(vals)]
    return float(vals.mean()) if len(vals) else None


def hac_t(series, lag):
    x = np.asarray(series, dtype=float)
    valid = np.isfinite(x)
    n = int(valid.sum())
    if n < 2:
        return None
    avg = x[valid].mean()
    z = np.where(valid, x - avg, 0.)
    variance = z @ z
    for k in range(1, min(lag+1, len(z))):
        variance += 2 * (1-k/(lag+1)) * (z[k:] @ z[:-k])
    return float(avg*n / np.sqrt(variance*n/(n-1))) if variance > 0 else None


def cross_section(scores, labels, gate=None):
    score_ok = np.isfinite(scores)
    label_ok = np.isfinite(labels)
    common = score_ok & label_ok
    x, y = midranks(scores[common]), midranks(labels[common])
    ic = None
    if len(x) >= 2 and np.ptp(x) and np.ptp(y):
        ic = float(np.corrcoef(x, y)[0, 1])
    names = np.flatnonzero(score_ok)
    ranked = names[np.argsort(scores[names], kind="stable")]
    row = dict(ic=ic, names=len(names), labelled=int(common.sum()))
    for j in range(5):
        group = ranked[j*len(ranked)//5:(j+1)*len(ranked)//5]
        r = mean(labels[group])
        row[f"q{j+1}_pct"] = None if r is None else 100*r
    if gate is not None:
        a,b = labels[label_ok & gate], labels[label_ok & ~gate]
        row.update(gate_true_n=len(a), gate_false_n=len(b))
        if len(a) and len(b):
            row.update(gate_return_diff_pp=100*(a.mean()-b.mean()),
                       gate_win_diff_pp=100*((a>0).mean()-(b>0).mean()))
    return row


def summarize(rows, horizon):
    def series(key):
        return np.asarray([r.get(key) for r in rows], dtype=float)
    ic = series("ic")
    valid = ic[np.isfinite(ic)]
    lag = horizon//5  # Phase 2 weekly Bartlett convention, including short trading weeks.
    result = dict(weeks=len(rows), ic_weeks=len(valid), ic=mean(ic),
                  icir=float(valid.mean()/valid.std(ddof=1)) if len(valid)>1 and valid.std(ddof=1)>0 else None,
                  positive_ic_pct=mean(100*(valid>0)), hac_lags=lag, ic_t_hac=hac_t(ic,lag),
                  names=sum(r["names"] for r in rows), labelled=sum(r["labelled"] for r in rows))
    q = [series(f"q{i}_pct") for i in range(1,6)]
    complete = np.all(np.isfinite(q),axis=0)
    levels = [mean(x[complete]) for x in q]
    result.update({f"q{i+1}_pct":v for i,v in enumerate(levels)})
    result.update(quintile_weeks=int(complete.sum()), monotone_increasing=bool(complete.any() and np.all(np.diff(levels)>=0)),
                  q5_q1_pp=mean((q[-1]-q[0])[complete]), spread_t_hac=hac_t(np.where(complete,q[-1]-q[0],np.nan), lag))
    if any("gate_true_n" in r for r in rows):
        for key in ("gate_return_diff_pp","gate_win_diff_pp"):
            result[key] = mean(series(key))
            result[key+"_hac_t"] = hac_t(series(key),lag)
    return result

