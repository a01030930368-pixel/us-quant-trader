"""Run a grid of strategy configs and rank them on out-of-sample results.

Ranking rules (see plan):
  * Metrics are computed separately for in-sample (IS) and out-of-sample (OOS).
  * Ranking uses OOS only; IS is shown so overfit configs (great IS, poor OOS)
    stand out.
  * Hard filters drop configs with too deep a drawdown or too few trades.
  * `robustness` = median OOS Sharpe of the same strategy's neighbouring
    parameter values; a lone spike in the grid scores low.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pandas as pd

from .backtest import run_backtest
from .metrics import summarize
from .strategies import REGISTRY


@dataclass
class Config:
    strategy: str
    params: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        p = ",".join(f"{k}={'+'.join(v) if isinstance(v, (list, tuple)) else v}"
                     for k, v in self.params.items() if k != "parts")
        return f"{self.strategy}({p})"


def expand_grid(strategy: str, fixed: dict, grid: dict) -> list[Config]:
    keys = list(grid)
    return [Config(strategy, {**fixed, **dict(zip(keys, vals))})
            for vals in itertools.product(*(grid[k] for k in keys))]


WEIGHTS = {"sharpe": 0.4, "calmar": 0.3, "cagr": 0.2, "robustness": 0.1}


def evaluate(prices: pd.DataFrame, configs: list[Config], split: str,
             cost_bps: float = 10.0, max_mdd: float = -0.30,
             min_trades: int = 10) -> pd.DataFrame:
    rows = []
    for cfg in configs:
        weights = REGISTRY[cfg.strategy](prices, **cfg.params)
        res = run_backtest(prices, weights, cost_bps=cost_bps)
        is_mask = res.returns.index < pd.Timestamp(split)
        oos_mask = ~is_mask
        is_m = summarize(res.returns[is_mask])
        oos_m = summarize(res.returns[oos_mask], res.turnover[oos_mask],
                          int((res.turnover[oos_mask] > 1e-9).sum()))
        row = {"label": cfg.label, "strategy": cfg.strategy,
               **{f"is_{k}": v for k, v in is_m.items()},
               **{f"oos_{k}": v for k, v in oos_m.items()},
               "total_trades": res.n_trades}
        row["_params"] = cfg.params
        rows.append(row)
    df = pd.DataFrame(rows)
    df["robustness"] = _robustness(df)
    df["passed"] = (df["oos_mdd"] >= max_mdd) & (df["total_trades"] >= min_trades)
    df["score"] = _score(df)
    return df.sort_values(["passed", "score"], ascending=[False, False]).reset_index(drop=True)


def _robustness(df: pd.DataFrame) -> pd.Series:
    """Median OOS Sharpe over configs of the same strategy that differ in exactly one param."""
    out = pd.Series(index=df.index, dtype=float)
    for i, row in df.iterrows():
        mine = row["_params"]
        peers = [row["oos_sharpe"]]
        for j, other in df[df["strategy"] == row["strategy"]].iterrows():
            if i == j:
                continue
            theirs = other["_params"]
            if mine.keys() == theirs.keys() and sum(mine[k] != theirs[k] for k in mine) == 1:
                peers.append(other["oos_sharpe"])
        out[i] = pd.Series(peers).median()
    return out


def _score(df: pd.DataFrame) -> pd.Series:
    """Weighted average of percentile ranks (0..1, higher is better)."""
    cols = {"sharpe": df["oos_sharpe"], "calmar": df["oos_calmar"],
            "cagr": df["oos_cagr"], "robustness": df["robustness"]}
    score = sum(WEIGHTS[k] * s.rank(pct=True) for k, s in cols.items())
    return score.round(4)
