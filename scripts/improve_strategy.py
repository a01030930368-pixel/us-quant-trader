"""Can the 50/50 blend get out of drawdowns faster and beat SPY since 2015?

Tests pre-declared variants of quant.strategies.momentum_blend:
  1. Each idea alone and combined, on real TQQQ/UPRO, judged on 2015~.
  2. Parameter grid for the trend filter + volatility cap (robustness).
  3. Walk-forward: each year pick the grid setting with the best Sharpe on the
     prior 8 years (simulated 3x from 2007), trade the next year with it.
  4. Cost stress, 5. sub-periods and year by year, 6. aggressive share.
All runs: next-day open fills, 15bp one-way unless stated.

Usage: python scripts/improve_strategy.py   (writes reports/improve.md)
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.backtest import run_backtest  # noqa: E402
from quant.data import add_leveraged, load_prices  # noqa: E402
from quant.metrics import monthly_returns, summarize  # noqa: E402
from quant.strategies import buy_and_hold, momentum_blend  # noqa: E402
from validate_strategy import UNIVERSE, fmt, with_real_leveraged  # noqa: E402

START = "2015-01-01"
COST = 15.0
ALL_LB = [21, 63, 126, 252]

VARIANTS = {
    "기준 50/50 (월 1회)": {},
    "주 1회 리밸런싱": {"rebalance": "W"},
    "여러 기간 모멘텀 평균": {"lookbacks": ALL_LB},
    "3배 ETF 추세필터 (200일선, 매일 확인)": {"trend_window": 200},
    "3배 바구니 변동성 상한 50% (매주 확인)": {"vol_target": 0.5},
    "채권 하락 시 현금": {"safe_filter": True},
    "추세필터 + 변동성 상한": {"trend_window": 200, "vol_target": 0.5},
    "추세필터 + 변동성 상한 + 주 1회": {"trend_window": 200, "vol_target": 0.5, "rebalance": "W"},
    "전부 적용": {"trend_window": 200, "vol_target": 0.5, "rebalance": "W",
                "lookbacks": ALL_LB, "safe_filter": True},
}
GRID = [{"trend_window": t, "vol_target": v, "rebalance": r}
        for t, v, r in itertools.product((100, 150, 200, 250), (0.3, 0.4, 0.5, 0.6), ("M", "W"))]


def underwater_months(r: pd.Series) -> int:
    """Longest stretch (in months) spent below a previous equity high."""
    eq = (1 + r).cumprod()
    under = eq < eq.cummax()
    longest = run = 0
    for flag in under.values:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return round(longest / 21)


def stats(r: pd.Series, turnover: pd.Series | None = None) -> dict:
    m = summarize(r)
    out = {"연수익": m["cagr"], "최대낙폭": m["mdd"], "Sharpe": m["sharpe"],
           "최악의 달": m["worst_month"], "최장 회복(개월)": underwater_months(r)}
    if turnover is not None:
        out["연 회전율"] = f"{turnover.sum() / (len(turnover) / 252):.1f}배"
    return out


def since(res, start=START):
    return res.returns.loc[start:], res.turnover.loc[start:]


def main():
    close = load_prices(UNIVERSE, "2007-01-01").dropna()
    opens = load_prices(UNIVERSE, "2007-01-01", field="Open").reindex(close.index)
    for lev, base in {"SPY3X": "SPY", "QQQ3X": "QQQ"}.items():
        close = add_leveraged(close, base, lev)
        gap = (opens[base] / close[base].shift(1) - 1).fillna(0.0)
        opens[lev] = close[lev].shift(1).bfill() * (1 + 3 * gap)
    rc, ro = with_real_leveraged(close, opens, "2010-03-01")

    def real(params, cost=COST, share=0.5):
        return run_backtest(rc, momentum_blend(rc, aggr_share=share, **params), cost, ro)

    def sim(params, cost=COST):
        return run_backtest(close, momentum_blend(close, **params), cost, opens)

    spy_real = run_backtest(rc, buy_and_hold(rc, "SPY"), COST, ro)
    spy_sim = run_backtest(close, buy_and_hold(close, "SPY"), COST, opens)
    spy = stats(*since(spy_real))
    out = ["# 50/50 개선 실험\n",
           f"실제 TQQQ/UPRO 데이터 {rc.index[0].date()} ~ {rc.index[-1].date()}, "
           f"성과는 {START[:4]}년 이후만 집계. 다음 날 시가 체결, 편도 {COST:g}bp.\n",
           "'SPY를 이겼다' = 연수익이 SPY보다 높고 최대낙폭이 SPY보다 얕음.\n"]

    # 1. Variants
    rows, cache = [], {}
    for name, params in VARIANTS.items():
        res = cache[name] = real(params)
        full = summarize(sim(params).returns)
        rows.append({"구분": name, **stats(*since(res)),
                     "전체(2007~,모의) 연수익": full["cagr"], "전체 최대낙폭": full["mdd"]})
    full = summarize(spy_sim.returns)
    rows.append({"구분": "SPY 단순보유", **spy, "전체(2007~,모의) 연수익": full["cagr"],
                 "전체 최대낙폭": full["mdd"]})
    out += ["## 1. 아이디어별 결과\n", fmt(pd.DataFrame(rows)), ""]

    # 2. Grid robustness
    rows = []
    for p in GRID:
        s = stats(since(real(p))[0])
        rows.append({"추세선(일)": p["trend_window"], "변동성 상한": f"{p['vol_target']:.0%}",
                     "리밸런싱": "주" if p["rebalance"] == "W" else "월", **s,
                     "SPY 이김": "예" if s["연수익"] > spy["연수익"] and s["최대낙폭"] > spy["최대낙폭"]
                     else "아니오"})
    grid = pd.DataFrame(rows)
    wins = (grid["SPY 이김"] == "예").sum()
    out += ["## 2. 추세필터 + 변동성 상한 파라미터 안정성\n",
            f"{len(grid)}개 조합 중 {wins}개가 SPY를 이김 "
            f"(연수익 중앙값 {grid['연수익'].median() * 100:.1f}%, "
            f"최대낙폭 중앙값 {grid['최대낙폭'].median() * 100:.1f}%).\n",
            fmt(grid), ""]

    # 3. Walk-forward over the grid on simulated 3x (needs 2007~ history)
    runs = [sim(p).returns for p in GRID]
    pieces, picks = [], []
    for year in range(2015, close.index[-1].year + 1):
        train = slice(pd.Timestamp(year - 8, 1, 1), pd.Timestamp(year - 1, 12, 31))
        best = max(range(len(GRID)), key=lambda i: summarize(runs[i].loc[train])["sharpe"])
        pieces.append(runs[best].loc[str(year)])
        p = GRID[best]
        picks.append({"연도": str(year), "선택": f"{p['trend_window']}일선 / 상한 "
                      f"{p['vol_target']:.0%} / {'주' if p['rebalance'] == 'W' else '월'}",
                      "그해 수익": float((1 + pieces[-1]).prod() - 1)})
    wf = pd.concat(pieces)
    base_sim = sim({}).returns.loc[wf.index]
    t = pd.DataFrame([{"구분": "워크포워드 (매년 재선택)", **stats(wf)},
                      {"구분": "기준 50/50", **stats(base_sim)},
                      {"구분": "SPY 단순보유", **stats(spy_sim.returns.loc[wf.index])}])
    out += ["## 3. 워크포워드 (2015~, 모의 3배, 직전 8년 Sharpe로 매년 선택)\n",
            fmt(t), "", fmt(pd.DataFrame(picks)), ""]

    # 4. Costs
    rows = []
    for name in ("기준 50/50 (월 1회)", "추세필터 + 변동성 상한", "추세필터 + 변동성 상한 + 주 1회"):
        for c in (15, 30, 45):
            res = real(VARIANTS[name], cost=c)
            rows.append({"구분": name, "편도 비용": f"{c}bp", **stats(*since(res))})
    out += ["## 4. 거래비용 민감도\n", fmt(pd.DataFrame(rows)), ""]

    # 5. Sub-periods and years
    show = ["기준 50/50 (월 1회)", "추세필터 + 변동성 상한", "추세필터 + 변동성 상한 + 주 1회",
            "전부 적용"]
    rows = []
    for a, b in (("2015", "2019"), ("2020", "2026")):
        for name in show:
            rows.append({"기간": f"{a}~{b}", "구분": name,
                         **stats(cache[name].returns.loc[a:b])})
        rows.append({"기간": f"{a}~{b}", "구분": "SPY 단순보유",
                     **stats(spy_real.returns.loc[a:b])})
    out += ["## 5. 기간별\n", fmt(pd.DataFrame(rows)), ""]
    yr = {n: (1 + cache[n].returns.loc[START:]).groupby(lambda d: d.year).prod() - 1 for n in show}
    yr["SPY"] = (1 + spy_real.returns.loc[START:]).groupby(lambda d: d.year).prod() - 1
    yearly = pd.DataFrame(yr)
    yearly.index = yearly.index.astype(str)
    yearly.index.name = "연도"
    out += ["### 연도별\n", fmt(yearly.reset_index()), ""]

    # 6. Aggressive share with the combined controls
    rows = []
    for sh in (0.3, 0.5, 0.7, 1.0):
        res = real(VARIANTS["추세필터 + 변동성 상한"], share=sh)
        rows.append({"공격 비중": f"{sh:.0%}", **stats(*since(res))})
    rows.append({"공격 비중": "SPY 보유", **spy})
    out += ["## 6. 추세필터 + 변동성 상한에서 공격 비중별\n", fmt(pd.DataFrame(rows)), ""]

    # Worst months, to see what the controls changed
    worst = {}
    for name in ("기준 50/50 (월 1회)", "추세필터 + 변동성 상한"):
        worst[name] = monthly_returns(cache[name].returns.loc[START:])
    worst["SPY"] = monthly_returns(spy_real.returns.loc[START:])
    w = pd.DataFrame(worst)
    w = w.loc[w["기준 50/50 (월 1회)"].nsmallest(8).index].sort_index()
    w.index = w.index.astype(str)
    w.index.name = "월"
    out += ["## 7. 기준 전략의 최악 8개월에 개선안은?\n", fmt(w.reset_index()), ""]

    # 8. Is the multi-window momentum gain robust to the choice of windows?
    rows = []
    for lbs in ([21, 63, 126, 252], [63, 126, 252], [21, 63, 126], [42, 126, 252],
                [21, 126, 252], [63, 189], [21, 42, 63]):
        for sh in (0.3, 0.5):
            s = stats(*since(real({"lookbacks": lbs}, share=sh)))
            rows.append({"기간 조합(거래일)": ",".join(map(str, lbs)), "공격 비중": f"{sh:.0%}", **s,
                         "SPY 이김": "예" if s["연수익"] > spy["연수익"] and s["최대낙폭"] > spy["최대낙폭"]
                         else "아니오"})
    out += ["## 8. 여러 기간 모멘텀 평균: 기간 조합을 바꿔도 유지되나?\n",
            fmt(pd.DataFrame(rows)), ""]

    Path("reports").mkdir(exist_ok=True)
    text = "\n".join(out)
    Path("reports/improve.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
