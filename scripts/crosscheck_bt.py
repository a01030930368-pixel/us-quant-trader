"""Cross-check our backtest engine against the bt library (pmorissette/bt).

Same prices, same daily target weights (the live canary_eem config via
runner.model_weights), same 10bp one-way cost. Three results:
  1. our engine (quant.backtest.run_backtest): holds the target weights
     every day, charges cost only when the target changes;
  2. bt, rebalanced to the target every day: the same holding assumption,
     so with zero cost the two must match (with costs bt also pays for the
     small daily trades back to target, so it comes out slightly lower);
  3. bt, trading only when the target changes and letting prices drift in
     between: how a real account behaves.
If 1 and 2 disagree, our arithmetic has a bug; 1 vs 3 shows what the
"constant weights" shortcut costs or adds.

Usage: python scripts/crosscheck_bt.py   (writes reports/crosscheck_bt.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.backtest import run_backtest  # noqa: E402
from quant.data import load_prices  # noqa: E402
from quant.metrics import cagr, max_drawdown, sharpe  # noqa: E402
from quant.trader.runner import BENCHMARK, DATA_TICKERS, TraderConfig, model_weights  # noqa: E402
from scripts.run_trader import STRATEGIES  # noqa: E402

COST_BPS = 10.0
START = "2009-01-01"
TOLERANCE_PP = 0.1  # engine vs bt-daily CAGR gap at zero cost that counts as a mismatch


def bt_returns(close: pd.DataFrame, w: pd.DataFrame, daily: bool, cost_bps: float) -> pd.Series:
    import bt

    target = w if daily else w[w.diff().abs().sum(axis=1).fillna(1.0) > 1e-9]
    algos = [bt.algos.WeighTarget(target), bt.algos.Rebalance()]
    if not daily:  # act only on days the target changes
        algos.insert(0, bt.algos.RunOnDate(*target.index))
    strat = bt.Strategy("s", algos)
    test = bt.Backtest(strat, close[w.columns], initial_capital=1_000_000.0,
                       integer_positions=False,
                       commissions=lambda q, p: abs(q * p) * cost_bps / 10_000)
    res = bt.run(test)
    return res.prices["s"].pct_change().dropna()


def compare(close: pd.DataFrame, cfg: TraderConfig, cost_bps: float = COST_BPS) -> tuple[pd.DataFrame, pd.Timestamp]:
    w = model_weights(close, cfg)
    invested = w.sum(axis=1) > 0
    start = invested.idxmax()
    ours = run_backtest(close[w.columns], w, cost_bps).returns
    series = {
        "우리 엔진": ours,
        "bt (매일 목표 비중)": bt_returns(close, w, True, cost_bps),
        "bt (바뀔 때만 매매)": bt_returns(close, w, False, cost_bps),
        "SPY 보유": close[BENCHMARK].pct_change(),
    }
    rows = {}
    for name, r in series.items():
        r = r[r.index > start].fillna(0.0)
        rows[name] = {"연수익률": cagr(r), "최대낙폭": max_drawdown(r), "샤프": sharpe(r),
                      "우리 엔진과 일별 상관": r.corr(ours[ours.index > start])}
    return pd.DataFrame(rows).T, start


def main() -> None:
    cfg = TraderConfig(**STRATEGIES["canary_eem"])
    tickers = sorted(set(DATA_TICKERS) | set(cfg.canary))
    close = load_prices(tickers, START, use_cache=False).dropna()
    table, start = compare(close, cfg)
    free, _ = compare(close, cfg, 0.0)  # pure arithmetic check: no costs on either side
    gap = (free.loc["우리 엔진", "연수익률"] - free.loc["bt (매일 목표 비중)", "연수익률"]) * 100
    ok = abs(gap) <= TOLERANCE_PP
    fmt = table.copy()
    for c in ("연수익률", "최대낙폭"):
        fmt[c] = fmt[c].map(lambda v: f"{v:.1%}")
    for c in ("샤프", "우리 엔진과 일별 상관"):
        fmt[c] = fmt[c].map(lambda v: f"{v:.3f}")
    lines = [
        "# 백테스트 엔진 교차 검증 (bt 라이브러리)", "",
        f"전략: EEM 카나리아 + 4분할 (지금 모의투자 설정), 기간 {start:%Y-%m-%d} ~ {close.index[-1]:%Y-%m-%d}, "
        f"수수료 편도 {COST_BPS / 100:.1f}%.", "",
        fmt.to_markdown(), "",
        f"**결과: {'일치' if ok else '불일치'}** (수수료 0으로 돌렸을 때 우리 엔진과 bt(매일 목표 비중)의 "
        f"연수익률 차이 {gap:+.3f}%p, 허용 ±{TOLERANCE_PP}%p).", "",
        "- '매일 목표 비중'은 우리 엔진과 같은 가정이라 거의 같아야 해요. 차이가 크면 우리 계산에 버그가 있다는 뜻이에요.",
        "- '바뀔 때만 매매'는 실제 계좌처럼 사이사이 가격이 움직이게 둔 결과예요. 우리 엔진과의 차이가 "
        "'매일 비중을 맞춘다'는 단순화의 영향이에요.",
    ]
    out = Path("reports")
    out.mkdir(exist_ok=True)
    (out / "crosscheck_bt.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
