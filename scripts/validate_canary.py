"""Is the EEM-only canary (aggressive 50%, 4 tranches) real or a lucky pick?

It was chosen after looking at 2015~ results, so this checks it where it was
not chosen:
  1. 2007-2014 (simulated 3x before 2010), never used for picking it.
  2. Other "risk appetite" canaries in its place (VWO, EFA, IWM, HYG).
  3. Other momentum formulas for the signal.
  4. Aggressive share, 5. costs, 6. how often it switches, 7. years.
All candidates run as 4 tranches (month end, +5, +10, +15 trading days),
next-day open fills, 15bp one-way unless stated.

Usage: python scripts/validate_canary.py   (writes reports/canary.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quant.backtest import run_backtest  # noqa: E402
from quant.data import add_leveraged, load_prices  # noqa: E402
from quant.metrics import summarize  # noqa: E402
from quant.strategies import REGISTRY, buy_and_hold, canary_gate  # noqa: E402
from research_strategy import beats, stats  # noqa: E402
from validate_strategy import UNIVERSE, fmt, with_real_leveraged  # noqa: E402

COST = 15.0
OOS = ("2007-01-01", "2014-12-31")  # not used when the EEM canary was picked
NEW = "2015-01-01"
LB = [21, 63, 126, 252]
T = {"tranches": 4, "spacing": 5}
EXTRA = ["EEM", "AGG", "VWO", "IWM", "HYG"]
EEM = {"lookbacks": LB, **T, "strategy": "canary_gate", "canary": ["EEM"]}

CASES = {
    "지금 전략 (4분할)": {"lookbacks": LB, **T},
    "카나리아 EEM·AGG": {**EEM, "canary": ["EEM", "AGG"]},
    "카나리아 EEM만": EEM,
}


def main():
    tickers = list(dict.fromkeys(UNIVERSE + EXTRA))
    close = load_prices(tickers, "2007-01-01").dropna()
    opens = load_prices(tickers, "2007-01-01", field="Open").reindex(close.index)
    for lev, base in {"SPY3X": "SPY", "QQQ3X": "QQQ"}.items():
        close = add_leveraged(close, base, lev)
        gap = (opens[base] / close[base].shift(1) - 1).fillna(0.0)
        opens[lev] = close[lev].shift(1).bfill() * (1 + 3 * gap)
    rc, ro = with_real_leveraged(close, opens, "2010-03-01")

    def both(params, cost=COST):
        """(simulated 3x full history, real 3x from 2010) returns."""
        s = run_backtest(close, REGISTRY["tranched"](close, **params), cost, opens).returns
        r = run_backtest(rc, REGISTRY["tranched"](rc, **params), cost, ro).returns
        return s, r

    def row(name, params, cost=COST):
        s, r = both(params, cost)
        o, n = stats(s.loc[OOS[0]:OOS[1]]), stats(r.loc[NEW:])
        return {"구분": name, "07~14 연수익": o["연수익"], "07~14 낙폭": o["최대낙폭"],
                "07~14 Sharpe": f"{o['Sharpe']:.2f}", "15~ 연수익": n["연수익"], "15~ 낙폭": n["최대낙폭"],
                "15~ Sharpe": f"{n['Sharpe']:.2f}", "15~ 최악의 달": n["최악의 달"],
                "_o": o, "_n": n}

    spy_s = run_backtest(close, buy_and_hold(close, "SPY"), COST, opens).returns
    spy_r = run_backtest(rc, buy_and_hold(rc, "SPY"), COST, ro).returns
    qqq_s = run_backtest(close, buy_and_hold(close, "QQQ"), COST, opens).returns
    qqq_r = run_backtest(rc, buy_and_hold(rc, "QQQ"), COST, ro).returns
    spy_o, spy_n = stats(spy_s.loc[OOS[0]:OOS[1]]), stats(spy_r.loc[NEW:])

    def table(rows):
        for r in rows:
            o, n = r.pop("_o", None), r.pop("_n", None)
            if o is not None:
                r["07~14 SPY 이김"] = beats(o, spy_o)
                r["15~ SPY 이김"] = beats(n, spy_n)
        return fmt(pd.DataFrame(rows))

    def bench():
        out = []
        for name, s, r in (("SPY 단순보유", spy_s, spy_r), ("QQQ 단순보유", qqq_s, qqq_r)):
            o, n = stats(s.loc[OOS[0]:OOS[1]]), stats(r.loc[NEW:])
            out.append({"구분": name, "07~14 연수익": o["연수익"], "07~14 낙폭": o["최대낙폭"],
                        "07~14 Sharpe": f"{o['Sharpe']:.2f}", "15~ 연수익": n["연수익"],
                        "15~ 낙폭": n["최대낙폭"], "15~ Sharpe": f"{n['Sharpe']:.2f}",
                        "15~ 최악의 달": n["최악의 달"]})
        return out

    out = ["# 카나리아 EEM만 검증\n",
           f"데이터 {close.index[0].date()} ~ {close.index[-1].date()}. "
           "2007~2014는 모의 3배 ETF(이 설정을 고를 때 안 본 구간), 2015~는 실제 TQQQ/UPRO.\n",
           f"모두 4분할, 다음 날 시가 체결, 편도 {COST:g}bp. "
           "'SPY 이김' = 같은 구간에서 연수익이 더 높고 최대낙폭이 더 얕음.\n"]

    # 1. Out of sample
    rows = [row(n, p) for n, p in CASES.items()] + bench()
    out += ["## 1. 고를 때 안 본 구간(2007~2014)에서도 좋은가\n", table(rows), ""]

    # 2. Other canaries
    rows = [row(f"신호 {c}만", {**EEM, "canary": [c]}) for c in ("EEM", "VWO", "EFA", "IWM", "HYG")]
    rows.append(row("신호 EEM·VWO", {**EEM, "canary": ["EEM", "VWO"]}))
    out += ["## 2. 비슷한 신호로 바꿔도 되는가\n",
            "VWO는 EEM과 같은 신흥국 ETF(다른 회사). 비슷하게 나오면 'EEM 데이터 운'이 아니라는 뜻입니다.\n",
            table(rows), ""]

    # 3. Momentum formula
    rows = [row(f"EEM, {lbl}", {**EEM, "signal": sig})
            for sig, lbl in (("13612W", "가중 1·3·6·12개월 (기본)"), ("12m", "12개월 수익률"),
                             ("avg", "1·3·6·12개월 단순평균"))]
    out += ["## 3. 신호 계산법을 바꿔도 되는가\n", table(rows), ""]

    # 4. Aggressive share
    rows = [row(f"공격 {sh:.0%}", {**EEM, "aggr_share": sh}) for sh in (0.3, 0.4, 0.5, 0.6, 0.7)]
    out += ["## 4. 공격 비중별 (EEM 신호)\n", table(rows), ""]

    # 5. Costs
    rows = [row(f"편도 {c}bp", EEM, cost=c) for c in (15, 30, 45)]
    out += ["## 5. 거래비용 (EEM 신호)\n", table(rows), ""]

    # 6. How often does it step aside?
    ends = pd.Series(close.index, index=close.index).groupby(close.index.to_period("M")).last()
    w = canary_gate(close, canary=["EEM"], lookbacks=LB)
    off = (w.loc[ends.values, "IEF"] > 0.99).astype(int)
    flips = int(off.diff().abs().sum())
    yearly_off = off.groupby(off.index.year).mean()
    out += ["## 6. 얼마나 자주 피하나 (EEM 신호, 월말 기준)\n",
            f"전체 {len(off)}개월 중 {off.sum()}개월({off.mean():.0%})을 전부 채권(IEF)으로 보냈고, "
            f"켜고 끈 횟수는 {flips}번(연 {flips / (len(off) / 12):.1f}번)입니다.\n",
            fmt(pd.DataFrame({"연도": yearly_off.index.astype(str),
                              "피난한 달 비율": yearly_off.values})), ""]

    # 7. Years (simulated before 2015, real after)
    yr = {}
    for name, p in CASES.items():
        s, r = both(p)
        chain = pd.concat([s.loc[:"2014"], r.loc[NEW:]])
        yr[name] = (1 + chain).groupby(lambda d: d.year).prod() - 1
    yr["SPY"] = (1 + pd.concat([spy_s.loc[:"2014"], spy_r.loc[NEW:]])).groupby(
        lambda d: d.year).prod() - 1
    yearly = pd.DataFrame(yr)
    yearly.index = yearly.index.astype(str)
    yearly.index.name = "연도"
    out += ["## 7. 연도별\n", fmt(yearly.reset_index()), ""]

    # 8. Same periods and costs as the S&P 1500 four-sleeve stock blend
    # (other project): 2010-2017 / 2018~, 25bp one-way.
    rows = []
    for name, p in (("EEM 카나리아 (지금 모의투자)", EEM), ("지금 전략 이전 버전", CASES["지금 전략 (4분할)"])):
        s, r = both(p, cost=25.0)
        chain = pd.concat([s.loc[:"2017"], r.loc["2018":]])
        spy_m = (1 + pd.concat([spy_s.loc[:"2017"], spy_r.loc["2018":]])).resample("ME").prod() - 1
        mine_m = (1 + chain).resample("ME").prod() - 1
        for label, sl in (("2010~2017", slice("2010", "2017")), ("2018~", slice("2018", None)),
                          ("2010~ 전체", slice("2010", None))):
            st = stats(chain.loc[sl])
            rows.append({"전략": name, "구간": label, "연수익": st["연수익"], "최대낙폭": st["최대낙폭"],
                         "Sharpe": st["Sharpe"], "최악의 달": st["최악의 달"],
                         "수익 난 달": st["수익 난 달"],
                         "SPY와 월 상관": f"{mine_m.loc[sl].corr(spy_m.loc[sl]):.2f}"})
    out += ["## 8. 다른 프로젝트의 4종 혼합과 같은 조건 (편도 25bp)\n",
            "4종 혼합(S&P 1500 개별주): 2010~2017 연 13.7% / -21.5% / 샤프 0.85, "
            "2018~ 연 20.4% / -34.7% / 샤프 0.96, S&P500과 월 상관 0.76.\n",
            fmt(pd.DataFrame(rows)), ""]

    Path("reports").mkdir(exist_ok=True)
    text = "\n".join(out).replace("| nan ", "| -   ")
    Path("reports/canary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
