"""Several strategies in one account: split the money, rebalance monthly.

Strategies that rise and fall at different times smooth each other out, so a
mix can lose less in a bad stretch than any one of them. Each strategy runs
in its own part of the account exactly as in its own chapter; at the start of
every month the parts are set back to their target shares. Moving money
between parts means selling in one and buying in another, so the moved amount
pays the trading cost twice.

Mixes, fixed before the first run (doc chapter 25). The parts are the plain
base rule of each of the three signal families that made money both before
and after 2018, not the best-looking variant of each:
- 혼합 3종: gross profitability top 20 (ch.22), earnings surprise PEAD
  (ch.19), insider purchase top 10 (ch.24), a third each.
- 혼합 4종: the same three plus weekly momentum, a quarter each, as a stand-in
  for running them next to the user's existing momentum app.
"""
from __future__ import annotations

import pandas as pd


def blend(returns: dict[str, pd.Series], weights: dict[str, float], cost_bps: float) -> pd.Series:
    """Daily returns of an account split between strategies and reset to `weights` each month."""
    r = pd.DataFrame(returns).fillna(0.0)
    w = pd.Series(weights, dtype=float)
    w = w / w.sum()
    cost = cost_bps / 1e4
    month = r.index.to_period("M")
    held = w.copy()
    out = []
    for k, (day, row) in enumerate(r.iterrows()):
        if k == 0 or month[k] != month[k - 1]:
            moved = (held - w).abs().sum() / 2              # share of the account moved between parts
            held = w.copy()
            fee = moved * 2 * cost
        else:
            fee = 0.0
        grown = held * (1 + row[w.index])
        total = grown.sum()
        out.append(total / held.sum() - 1 - fee)
        held = grown / total
    return pd.Series(out, index=r.index)
