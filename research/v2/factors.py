"""Causal factor corrections for the signed PIT panels."""

import numpy as np

from core.factor_court import midranks


def value_scores_v2(pe, dividend):
    pe, dividend = np.asarray(pe, dtype=float), np.asarray(dividend, dtype=float)
    if pe.shape != dividend.shape or pe.ndim != 1:
        raise ValueError("Expected aligned, one-dimensional PE and dividend inputs")
    eligible = np.isfinite(pe) & (pe > 0)
    ep, dv = (np.full(len(pe), np.nan) for _ in range(2))
    ep[eligible] = midranks(1 / pe[eligible])
    observed = dividend[eligible]
    dv[eligible] = midranks(np.maximum(0, np.where(np.isfinite(observed), observed, 0)))
    return ep, dv, (ep + dv) / 2


def low_turnover(turnover, i):
    """Return the negated 20-session mean, requiring 15 finite prior observations."""
    if i < 20:
        return np.full(turnover.shape[1], np.nan)
    sample = turnover[i - 20:i]
    valid = np.isfinite(sample)
    count = valid.sum(axis=0)
    return np.divide(-np.where(valid, sample, 0).sum(axis=0), count,
                     out=np.full(len(count), np.nan), where=count >= 15)


class OpenSessionMomentum:
    def __init__(self, adjusted, opened):
        self.age = np.cumsum(opened, axis=0)
        self.opened = opened
        self.history = [adjusted[opened[:, c], c] for c in range(opened.shape[1])]

    def score(self, i, c, lag=240, skip=20):
        if not 0 <= skip < lag:
            raise ValueError("Invalid momentum endpoints")
        age = self.age[i, c]
        if not self.opened[i, c] or age <= lag:
            return np.nan
        series = self.history[c]
        first, last = series[age - lag - 1], series[age - skip - 1]
        if not (np.isfinite(first) and np.isfinite(last) and first > 0 and last > 0):
            return np.nan
        return last / first - 1

    def rank(self, i, columns, spec):
        if not spec.lag:
            return tuple(c for c in columns if self.opened[i, c])
        rows = [(self.score(i, c, spec.lag, spec.skip), c) for c in columns]
        rows = [(score, c) for score, c in rows
                if np.isfinite(score) and (not spec.positive or score > 0)]
        return tuple(c for _, c in sorted(rows, key=lambda row: (-row[0], row[1]))[:spec.slots])


def stock_order(tape, member, i, *, composite):
    if i == 0 or composite and i < 20:
        return ()
    mask = member[i] & tape.opened[i] & tape.basic[i - 1]
    if composite:
        mask &= tape.warm[i]
    names = np.flatnonzero(mask)
    panel = tape.panel
    score = value_scores_v2(panel["pe_ttm"][i - 1, names], panel["dv_ttm"][i - 1, names])[2]
    keep = np.isfinite(score)
    if composite:
        turn = low_turnover(panel["turnover_rate_f"], i)[names]
        keep &= np.isfinite(turn)
        ranked = (midranks(score[keep]) + midranks(turn[keep])) / 2
    else:
        ranked = score[keep]
    return tuple(map(int, names[keep][np.argsort(-ranked, kind="stable")]))
