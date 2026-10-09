"""New research directions for the paper-trading strategy (momentum_blend_multi).

Earlier experiments (scripts/improve_strategy.py) showed that cutting losses
sooner with trend filters, volatility caps or weekly checks makes the blend
worse. This script tries different angles, each pre-declared below:
  A. Tranches: split the money into parts that rebalance on different days.
  B. Core + satellite: always hold some QQQ/SPY, run the strategy on the rest.
  C. Canary gate: move to bonds when emerging markets / bonds momentum turns.
  D. Wider stable universe: add sector ETFs and emerging markets.
  E. 2x instead of 3x ETFs in the aggressive sleeve (QLD/SSO).
Judged on real TQQQ/UPRO from 2015, next-day open fills, 15bp one-way.

Usage: python scripts/research_strategy.py   (writes reports/research.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quant.backtest import run_backtest  # noqa: E402
from quant.data import add_leveraged, load_prices  # noqa: E402
from quant.metrics import monthly_returns, summarize  # noqa: E402
from quant.strategies import REGISTRY, STABLE, buy_and_hold  # noqa: E402
from improve_strategy import underwater_months  # noqa: E402
from validate_strategy import UNIVERSE, fmt, with_real_leveraged  # noqa: E402

START = "2015-01-01"
COST = 15.0
LB = [21, 63, 126, 252]
SECTORS = ["XLK", "XLV", "XLP", "XLE", "XLF", "XLU", "EEM"]
EXTRA = ["AGG", "QLD", "SSO"] + SECTORS
BASE = {"lookbacks": LB}  # the strategy now in paper trading

# name -> (strategy, params). All default to the 50/50 multi-window blend.
VARIANTS = {
    "기준: 지금 모의투자 전략": ("momentum_blend", BASE),
    "A. 4분할 (5거래일 간격)": ("tranched", {**BASE, "tranches": 4, "spacing": 5}),
    "A. 2분할 (10거래일 간격)": ("tranched", {**BASE, "tranches": 2, "spacing": 10}),
    "B. QQQ 30% 고정 + 전략 70%": ("core_satellite", {**BASE, "core": "QQQ", "core_share": 0.3}),
    "B. SPY 50% 고정 + 전략 50%": ("core_satellite", {**BASE, "core": "SPY", "core_share": 0.5}),
    "C. 카나리아 (EEM·AGG)": ("canary_gate", BASE),
    "D. 안정 바구니 확장 (섹터+신흥국)": ("momentum_blend",
                                 {**BASE, "stable_assets": STABLE + SECTORS}),
    "E. 공격 바구니 2배 ETF": ("momentum_blend",
                         {**BASE, "aggr_assets": ["QLD", "SSO", "GLD", "TLT"]}),
    "A+B. 4분할 + QQQ 30%": ("core_satellite", {**BASE, "core": "QQQ", "core_share": 0.3,
                                             "strategy": "tranched", "tranches": 4,
                                             "spacing": 5}),
    "A+C. 4분할 + 카나리아": ("canary_gate", {**BASE, "strategy": "tranched", "tranches": 4,
                                         "spacing": 5}),
}


def stats(r: pd.Series) -> dict:
    m = summarize(r)
    mo = monthly_returns(r)
    return {"연수익": m["cagr"], "최대낙폭": m["mdd"], "Sharpe": m["sharpe"],
            "수익 난 달": m["month_win_rate"], "월수익 표준편차": float(mo.std()),
            "최악의 달": m["worst_month"], "최장 회복(개월)": underwater_months(r)}


def beats(s: dict, spy: dict) -> str:
    return "예" if s["연수익"] > spy["연수익"] and s["최대낙폭"] > spy["최대낙폭"] else "아니오"


def main():
    tickers = list(dict.fromkeys(UNIVERSE + EXTRA))  # no duplicate columns
    close = load_prices(tickers, "2007-01-01").dropna()
    opens = load_prices(tickers, "2007-01-01", field="Open").reindex(close.index)
    for lev, base in {"SPY3X": "SPY", "QQQ3X": "QQQ"}.items():
        close = add_leveraged(close, base, lev)
        gap = (opens[base] / close[base].shift(1) - 1).fillna(0.0)
        opens[lev] = close[lev].shift(1).bfill() * (1 + 3 * gap)
    rc, ro = with_real_leveraged(close, opens, "2010-03-01")

    def real(strat, params, cost=COST):
        return run_backtest(rc, REGISTRY[strat](rc, **params), cost, ro)

    def sim(strat, params):
        return run_backtest(close, REGISTRY[strat](close, **params), COST, opens)

    spy_real = run_backtest(rc, buy_and_hold(rc, "SPY"), COST, ro).returns
    qqq_real = run_backtest(rc, buy_and_hold(rc, "QQQ"), COST, ro).returns
    spy = stats(spy_real.loc[START:])
    out = ["# 새 연구 방향 1차 실험\n",
           f"실제 TQQQ/UPRO 데이터 {rc.index[0].date()} ~ {rc.index[-1].date()}, "
           f"성과는 {START[:4]}년 이후만 집계. 다음 날 시가 체결, 편도 {COST:g}bp.\n",
           "'SPY 이김' = 연수익이 SPY보다 높고 최대낙폭이 SPY보다 얕음.\n"]

    # 1. Each direction
    rows, cache = [], {}
    for name, (strat, params) in VARIANTS.items():
        res = cache[name] = real(strat, params)
        r = res.returns.loc[START:]
        s = stats(r)
        full = summarize(sim(strat, params).returns)
        rows.append({"구분": name, **s,
                     "2015~19 연수익": summarize(res.returns.loc["2015":"2019"])["cagr"],
                     "2020~ 연수익": summarize(res.returns.loc["2020":])["cagr"],
                     "연 회전율": f"{res.turnover.loc[START:].sum() / (len(r) / 252):.1f}배",
                     "전체(2007~,모의) 연수익": full["cagr"], "전체 최대낙폭": full["mdd"],
                     "SPY 이김": beats(s, spy)})
    for name, r in (("SPY 단순보유", spy_real), ("QQQ 단순보유", qqq_real)):
        rows.append({"구분": name, **stats(r.loc[START:]),
                     "2015~19 연수익": summarize(r.loc["2015":"2019"])["cagr"],
                     "2020~ 연수익": summarize(r.loc["2020":])["cagr"]})
    out += ["## 1. 방향별 결과 (2015~)\n", fmt(pd.DataFrame(rows)), ""]

    # 2. Tranche settings: is the effect stable, or one lucky setting?
    rows = []
    for n in (2, 3, 4, 6):
        for gap in (3, 5, 7):
            if (n - 1) * gap > 20:
                continue
            s = stats(real("tranched", {**BASE, "tranches": n, "spacing": gap}).returns.loc[START:])
            rows.append({"분할 수": n, "간격(거래일)": gap, **s, "SPY 이김": beats(s, spy)})
    out += ["## 2. 분할 리밸런싱 설정별\n", fmt(pd.DataFrame(rows)), ""]

    # 2b. Single-tranche offsets: how much does the rebalance day alone matter?
    rows = []
    for off in (0, 3, 5, 8, 10, 15):
        s = stats(real("momentum_blend", {**BASE, "offset": off}).returns.loc[START:])
        rows.append({"리밸런싱일": "월말" if off == 0 else f"월말+{off}거래일", **s})
    out += ["### 리밸런싱 날짜만 바꾸면 (분할 없이)\n",
            "이 표의 편차가 크면 지금 성과에 '날짜 운'이 섞여 있다는 뜻입니다.\n",
            fmt(pd.DataFrame(rows)), ""]

    # 3. Core share
    rows = []
    for core in ("QQQ", "SPY"):
        for sh in (0.2, 0.3, 0.4, 0.5):
            s = stats(real("core_satellite", {**BASE, "core": core, "core_share": sh})
                      .returns.loc[START:])
            rows.append({"고정 보유": f"{core} {sh:.0%}", **s, "SPY 이김": beats(s, spy)})
    out += ["## 3. 고정 보유 비중별\n", fmt(pd.DataFrame(rows)), ""]

    # 4. Years
    show = list(VARIANTS)
    yr = {n: (1 + cache[n].returns.loc[START:]).groupby(lambda d: d.year).prod() - 1 for n in show}
    yr["SPY"] = (1 + spy_real.loc[START:]).groupby(lambda d: d.year).prod() - 1
    yearly = pd.DataFrame(yr)
    yearly.index = yearly.index.astype(str)
    yearly.index.name = "연도"
    out += ["## 4. 연도별\n", fmt(yearly.reset_index()), ""]

    # 5. Cost stress for the variants that trade more often
    rows = []
    for name in ("기준: 지금 모의투자 전략", "A. 4분할 (5거래일 간격)", "A+B. 4분할 + QQQ 30%"):
        strat, params = VARIANTS[name]
        for c in (15, 30):
            rows.append({"구분": name, "편도 비용": f"{c}bp",
                         **stats(real(strat, params, cost=c).returns.loc[START:])})
    out += ["## 5. 거래비용 2배일 때\n", fmt(pd.DataFrame(rows)), ""]

    # 6. Round 2: the canary gate cut drawdowns the most. Is it robust, and
    # can the drawdown it saves buy a bigger aggressive share?
    rows = []
    cases = {
        "카나리아 기본 (공격 50%)": {},
        "공격 60%": {"aggr_share": 0.6},
        "공격 70%": {"aggr_share": 0.7},
        "공격 100%": {"aggr_share": 1.0},
        "+ QQQ 30% (위험 신호 땐 함께 피난)": {"strategy": "core_satellite", "core": "QQQ", "core_share": 0.3},
        "+ 2배 ETF": {"aggr_assets": ["QLD", "SSO", "GLD", "TLT"]},
        "+ 2배 ETF, 공격 100%": {"aggr_assets": ["QLD", "SSO", "GLD", "TLT"], "aggr_share": 1.0},
        "피난처 = 현금": {"safe": None},
        "신호 EEM만": {"canary": ["EEM"]},
        "신호 AGG만": {"canary": ["AGG"]},
        "신호 EEM·AGG·EFA": {"canary": ["EEM", "AGG", "EFA"]},
    }
    for name, extra in cases.items():
        res = real("canary_gate", {**BASE, **extra})
        s = stats(res.returns.loc[START:])
        full = summarize(sim("canary_gate", {**BASE, **extra}).returns)
        rows.append({"구분": name, **s,
                     "2015~19 연수익": summarize(res.returns.loc["2015":"2019"])["cagr"],
                     "2020~ 연수익": summarize(res.returns.loc["2020":])["cagr"],
                     "전체(2007~,모의) 연수익": full["cagr"], "전체 최대낙폭": full["mdd"],
                     "SPY 이김": beats(s, spy)})
    out += ["## 6. 카나리아 심화\n", fmt(pd.DataFrame(rows)), ""]

    rows = []
    for off in (0, 3, 5, 8, 10, 15):
        for sh in (0.5, 0.7):
            s = stats(real("canary_gate", {**BASE, "offset": off, "aggr_share": sh})
                      .returns.loc[START:])
            rows.append({"리밸런싱일": "월말" if off == 0 else f"월말+{off}거래일",
                         "공격 비중": f"{sh:.0%}", **s, "SPY 이김": beats(s, spy)})
    out += ["### 카나리아: 리밸런싱 날짜 운 점검\n", fmt(pd.DataFrame(rows)), ""]

    Path("reports").mkdir(exist_ok=True)
    text = "\n".join(out).replace("nan%", "-").replace("| nan ", "| -   ")
    Path("reports/research.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
