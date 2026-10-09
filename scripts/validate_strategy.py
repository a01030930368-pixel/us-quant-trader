"""Stress-test the stable/aggressive blend before paper trading.

Checks, each written to reports/validation.md:
  1. Execution: signal-day close vs next-day open.
  2. Costs: 15bp vs 30bp vs 45bp one-way.
  3. Real 3x ETFs: TQQQ/UPRO (2010~) instead of the simulated series.
  4. Walk-forward: each year re-pick the lookback using only the prior 8 years.
  5. Year-by-year returns vs SPY and QQQ.
  6. Rough after-tax return (22% on each year's gains).

Usage: python scripts/validate_strategy.py --aggr-share 0.5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.backtest import run_backtest  # noqa: E402
from quant.data import add_leveraged, load_prices  # noqa: E402
from quant.metrics import summarize  # noqa: E402
from quant.strategies import blend, buy_and_hold  # noqa: E402

UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "TLT", "IEF", "GLD"]
STABLE = ["SPY", "QQQ", "IWM", "EFA", "GLD"]
AGGR = ["QQQ3X", "SPY3X", "GLD", "TLT"]


def blend_weights(prices, share, lookback=63):
    return blend(prices, [
        ("momentum_rotation", {"assets": STABLE, "safe": "IEF", "lookback": lookback, "top_n": 1}, 1 - share),
        ("momentum_rotation", {"assets": AGGR, "safe": "IEF", "lookback": lookback, "top_n": 2}, share),
    ])


def row(name, res_or_returns, start=None):
    r = res_or_returns.returns if hasattr(res_or_returns, "returns") else res_or_returns
    if start:
        r = r[r.index >= pd.Timestamp(start)]
    m = summarize(r)
    return {"구분": name, "연수익": m["cagr"], "월평균": m["month_avg"], "최대낙폭": m["mdd"],
            "최악의 달": m["worst_month"], "Sharpe": m["sharpe"]}


def fmt(df):
    df = df.copy()
    for c in df.columns:
        if c in ("Sharpe",):
            df[c] = df[c].map(lambda v: f"{v:.2f}")
        elif df[c].dtype.kind == "f":
            df[c] = df[c].map(lambda v: f"{v * 100:.1f}%")
    return df.to_markdown(index=False)


def with_real_leveraged(close, opens, start):
    real_c = load_prices(["TQQQ", "UPRO"], start)
    real_o = load_prices(["TQQQ", "UPRO"], start, field="Open")
    c = close[close.index >= pd.Timestamp(start)].drop(columns=["QQQ3X", "SPY3X"])
    o = opens[opens.index >= pd.Timestamp(start)].drop(columns=["QQQ3X", "SPY3X"])
    c = c.join(real_c.rename(columns={"TQQQ": "QQQ3X", "UPRO": "SPY3X"}), how="inner")
    o = o.join(real_o.rename(columns={"TQQQ": "QQQ3X", "UPRO": "SPY3X"}), how="inner")
    return c.dropna(), o.reindex(c.index)


def walk_forward(close, opens, share, cost, train_years=8, first_test=2015,
                 lookbacks=(63, 126, 189, 252)):
    """For each test year pick the lookback with the best Sharpe on the prior
    `train_years`, then trade the test year with it. Returns chained returns
    and the picks."""
    runs = {lb: run_backtest(close, blend_weights(close, share, lb), cost, opens).returns
            for lb in lookbacks}
    pieces, picks = [], []
    for year in range(first_test, close.index[-1].year + 1):
        train = slice(pd.Timestamp(year - train_years, 1, 1), pd.Timestamp(year - 1, 12, 31))
        best = max(lookbacks, key=lambda lb: summarize(runs[lb].loc[train])["sharpe"])
        test = runs[best].loc[str(year)]
        pieces.append(test)
        picks.append({"연도": year, "선택된 lookback(거래일)": best,
                      "그해 수익": float((1 + test).prod() - 1)})
    return pd.concat(pieces), pd.DataFrame(picks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2007-01-01")
    ap.add_argument("--aggr-share", type=float, default=0.5)
    ap.add_argument("--cost-bps", type=float, default=15.0)
    args = ap.parse_args()
    share, cost = args.aggr_share, args.cost_bps
    name = f"혼합 {round((1 - share) * 100)}/{round(share * 100)}"

    close = load_prices(UNIVERSE, args.start).dropna()
    opens = load_prices(UNIVERSE, args.start, field="Open").reindex(close.index)
    for lev, base in {"SPY3X": "SPY", "QQQ3X": "QQQ"}.items():
        close = add_leveraged(close, base, lev)
        # Simulated open: carry the base ETF's overnight gap at 3x.
        gap = (opens[base] / close[base].shift(1) - 1).fillna(0.0)
        opens[lev] = close[lev].shift(1).bfill() * (1 + 3 * gap)

    w = blend_weights(close, share)
    out = [f"# {name} 정밀 검증\n",
           f"데이터 {close.index[0].date()} ~ {close.index[-1].date()}. "
           "특별한 언급이 없으면 전체 기간, 다음 날 시가 체결, 편도 비용 "
           f"{cost:g}bp 기준입니다.\n"]

    # 1. Execution timing
    t = pd.DataFrame([
        row("신호 당일 종가 체결 (기존 가정)", run_backtest(close, w, cost)),
        row("다음 날 시가 체결 (현실적)", run_backtest(close, w, cost, opens)),
    ])
    out += ["## 1. 체결 시점\n", fmt(t), ""]

    # 2. Costs
    t = pd.DataFrame([row(f"편도 {c}bp", run_backtest(close, w, c, opens)) for c in (15, 30, 45)])
    out += ["## 2. 거래비용 민감도\n", fmt(t), ""]

    # 3. Real leveraged ETFs
    rc, ro = with_real_leveraged(close, opens, "2010-03-01")
    t = pd.DataFrame([
        row("모의 3배 ETF", run_backtest(close, w, cost, opens), start=rc.index[0]),
        row("실제 TQQQ/UPRO", run_backtest(rc, blend_weights(rc, share), cost, ro)),
        row("SPY 단순보유", run_backtest(rc, buy_and_hold(rc, "SPY"), cost, ro)),
    ])
    sim_q = close["QQQ3X"].loc[rc.index].pct_change()
    real_q = rc["QQQ3X"].pct_change()
    yearly_gap = ((1 + sim_q).groupby(sim_q.index.year).prod()
                  - (1 + real_q).groupby(real_q.index.year).prod())
    out += [f"## 3. 실제 3배 ETF로 다시 계산 ({rc.index[0].date()}~)\n", fmt(t), "",
            f"모의 QQQ3X와 실제 TQQQ의 연간 수익률 차이: 평균 {yearly_gap.mean() * 100:+.1f}%p, "
            f"최대 {yearly_gap.abs().max() * 100:.1f}%p\n"]

    # 4. Walk-forward
    wf, picks = walk_forward(close, opens, share, cost)
    fixed = run_backtest(close, w, cost, opens).returns.loc[wf.index]
    spy = run_backtest(close, buy_and_hold(close, "SPY"), cost, opens).returns.loc[wf.index]
    t = pd.DataFrame([row("워크포워드 (매년 lookback 재선택)", wf),
                      row("고정 lookback 63", fixed), row("SPY 단순보유", spy)])
    out += [f"## 4. 워크포워드 ({wf.index[0].year}~, 직전 8년으로 매년 재선택)\n", fmt(t), "",
            fmt(picks), ""]

    # 5. Year by year
    yr = {}
    for label, res in [(name, run_backtest(close, w, cost, opens)),
                       ("SPY", run_backtest(close, buy_and_hold(close, "SPY"), cost, opens)),
                       ("QQQ", run_backtest(close, buy_and_hold(close, "QQQ"), cost, opens))]:
        yr[label] = (1 + res.returns).groupby(res.returns.index.year).prod() - 1
    yearly = pd.DataFrame(yr)
    yearly.index.name = "연도"
    out += ["## 5. 연도별 수익률\n", fmt(yearly.reset_index().astype({"연도": str})), ""]

    # 6. Rough tax
    blend_yr = yearly[name]
    after_tax = blend_yr.where(blend_yr <= 0, blend_yr * 0.78)
    years = len(blend_yr)
    pre = float((1 + blend_yr).prod() ** (1 / years) - 1)
    post = float((1 + after_tax).prod() ** (1 / years) - 1)
    out += ["## 6. 세후 대략치\n",
            f"매년 이익의 22%를 세금으로 낸다고 단순 가정(기본공제 250만 원, 손실 이월 미반영): "
            f"연 {pre * 100:.1f}% → 약 연 {post * 100:.1f}%\n"]

    Path("reports").mkdir(exist_ok=True)
    text = "\n".join(out)
    Path("reports/validation.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
