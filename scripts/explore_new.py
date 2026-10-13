"""Test brand-new strategy families against SPY and the paper strategy (EEM canary).

Rules fixed before looking at results:
  - Periods: 2006-07 (never used before), 2008-14, 2015~.
  - A variant passes a period when its Sharpe (vs T-bills) beats SPY's and its
    max drawdown is no more than 2 percentage points deeper than SPY's.
  - A family passes when its base settings pass all three periods AND at least
    60% of its nearby settings do too.
  - "Mix candidate": a 50/50 daily mix with the EEM canary has a higher Sharpe
    than the canary alone in all three periods, with drawdown no more than
    2 points deeper.
All runs: signal at the close, fill at the next open, one-way cost 25bp.

Usage: python scripts/explore_new.py   (writes reports/explore.md, reports/explore_returns.csv)
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant import new_strategies as ns  # noqa: E402
from quant.backtest import run_backtest  # noqa: E402
from quant.data import load_prices  # noqa: E402
from quant.metrics import monthly_returns  # noqa: E402
from scripts import audit_canary as ac  # noqa: E402

COST = 25.0
PERIODS = {"2006~07": ac.UNSEEN, "2008~14": ac.OOS, "2015~": ac.REAL}
EXTRA = ["DBC", "VNQ", "LQD", *ns.SECTORS]
MDD_SLACK = 0.02


def load_all():
    close, opens, irx, fx, real_c, real_o = ac.load()
    simT, simoT = ac.simulate_3x(close, opens, irx, "tbill")
    rc, ro = ac.splice_real(simT, simoT, real_c, real_o)
    ex_c = load_prices(EXTRA, "2004-01-01").reindex(rc.index)
    ex_o = load_prices(EXTRA, "2004-01-01", field="Open").reindex(rc.index)
    c = pd.concat([rc, ex_c], axis=1)
    o = pd.concat([ro, ex_o], axis=1)
    hl = ["SPY", "QQQ"]
    high = load_prices(hl, "2004-01-01", field="High").reindex(rc.index)
    low = load_prices(hl, "2004-01-01", field="Low").reindex(rc.index)
    vix = load_prices(["^VIX", "^VIX3M"], "2004-01-01").reindex(rc.index).ffill()
    canary = ac.run(rc, ro, ac.PAPER)
    return c, o, irx, high, low, vix, canary


def families(c, high, low, vix):
    """name -> (description, base params, list of params, builder)."""
    v, v3 = vix["^VIX"], vix["^VIX3M"]
    return {
        "단기 반등 RSI(2)": (
            "많이 떨어진 날(RSI2 낮음) 상승추세면 사고, 반등하면 팖. 쉬는 돈은 SHY",
            dict(asset="SPY", entry=10, trend=200),
            [dict(asset=a, entry=e, trend=t) for a, e, t in itertools.product(["SPY", "QQQ"], [5, 10, 15], [100, 200])],
            lambda p: ns.rsi_dip(c, **p)),
        "단기 반등 IBS": (
            "그날 저가 근처에서 끝나면 사고, 고가 근처에서 끝나면 팖. 쉬는 돈은 SHY",
            dict(asset="SPY", entry=0.2, exit_=0.7),
            [dict(asset=a, entry=e, exit_=x) for a, e, x in itertools.product(["SPY", "QQQ"], [0.1, 0.2, 0.3], [0.6, 0.7, 0.8])],
            lambda p: ns.ibs_dip(c, high, low, **p)),
        "월말·월초 효과": (
            "월말 며칠~월초 며칠만 SPY, 나머지는 IEF",
            dict(before=1, after=3),
            [dict(before=b, after=a) for b, a in itertools.product([1, 2, 3], [2, 3, 4])],
            lambda p: ns.turn_of_month(c, **p)),
        "여러 자산 추세추종": (
            "주식·채권·금·원자재·리츠 9개 중 오르는 것만 변동성 반비례로, 나머지는 SHY. 월 1회, 4분할",
            dict(lookback=252, vol_window=63),
            [dict(lookback=lb, vol_window=vw) for lb, vw in itertools.product([126, 189, 252], [21, 63, 126])],
            lambda p: ns.trend_parity(c, **p)),
        "VIX 기간구조": (
            "단기 공포지수(VIX)가 3개월 VIX보다 낮으면 SPY, 높으면 IEF. 매일 판단",
            dict(threshold=1.0, smooth=5),
            [dict(threshold=t, smooth=s) for t, s in itertools.product([0.95, 1.0, 1.05], [1, 5, 10])],
            lambda p: ns.vix_term(c, v, v3, **p)),
        "고정 배분": (
            "올웨더(SPY30/TLT40/IEF15/GLD7.5/DBC7.5) 등 비중 고정",
            dict(mix="all_weather"),
            [dict(mix=m) for m in ns.STATIC],
            lambda p: ns.static_mix(c, **p)),
        "저변동성 업종": (
            "9개 업종 ETF 중 가장 덜 흔들린 업종 몇 개를 균등 보유. 월 1회, 4분할",
            dict(top_n=3, vol_window=126),
            [dict(top_n=n, vol_window=vw) for n, vw in itertools.product([2, 3, 4], [63, 126, 252])],
            lambda p: ns.low_vol_sectors(c, **p)),
    }


def stats(r, irx):
    m = ac.metrics(r, irx)
    return m["연수익(CAGR)"], m["최대낙폭"], m["샤프"]


def passes(r, ref, irx) -> list[bool]:
    out = []
    for p in PERIODS.values():
        _, mdd, sh = stats(ac.per(r, p), irx)
        _, mdd_ref, sh_ref = stats(ac.per(ref, p), irx)
        out.append(sh > sh_ref and mdd >= mdd_ref - MDD_SLACK)
    return out


def pct(x):
    return f"{x * 100:.1f}%"


def main():
    c, o, irx, high, low, vix, canary = load_all()
    spy = ac.fixed(c, o, {"SPY": 1.0})
    fams = families(c, high, low, vix)
    vix_start = vix["^VIX3M"].first_valid_index()

    out = ["# 새 전략 계열 탐색 (1차)\n",
           f"데이터 {c.index[0].date()} ~ {c.index[-1].date()}. 신호는 종가, 체결은 다음 날 시가, 편도 비용 25bp. "
           "샤프는 단기국채 금리를 뺀 기준.\n",
           "판정 기준(결과를 보기 전에 고정): 세 구간(2006~07 / 2008~14 / 2015~) 모두 SPY보다 샤프가 높고 "
           "최대낙폭이 SPY보다 2%p 넘게 깊지 않아야 통과. 기본 설정이 통과하고 근처 설정의 60% 이상도 통과해야 '계열 통과'. "
           "카나리아와 50:50으로 섞었을 때 세 구간 모두 카나리아 단독보다 샤프가 높고 낙폭이 2%p 넘게 깊지 않으면 '혼합 후보'.\n",
           f"참고: VIX3M 데이터 시작일 {vix_start.date() if vix_start is not None else '없음'} (그 전엔 SPY 보유로 처리). "
           "DBC는 2006-02부터라 그 전엔 다른 자산으로 대체.\n"]

    rows, detail, returns = [], [], {"SPY": spy, "EEM 카나리아": canary}
    for name, (desc, base, grid, build) in fams.items():
        base_w = build(base)
        bt = run_backtest(c, base_w, COST, o)
        r = bt.returns
        r10 = run_backtest(c, base_w, 10.0, o).returns
        returns[name] = r
        base_pass = passes(r, spy, irx)
        grid_pass = [all(passes(run_backtest(c, build(p), COST, o).returns, spy, irx)) for p in grid]
        share = sum(grid_pass) / len(grid_pass)
        mix = 0.5 * r + 0.5 * canary
        mix_ok = []
        for p in PERIODS.values():
            _, md_m, sh_m = stats(ac.per(mix, p), irx)
            _, md_c, sh_c = stats(ac.per(canary, p), irx)
            mix_ok.append(sh_m > sh_c and md_m >= md_c - MDD_SLACK)
        corr = monthly_returns(ac.per(r, ("2006-01-01", None))).corr(
            monthly_returns(ac.per(canary, ("2006-01-01", None))))
        verdict = "통과" if all(base_pass) and share >= 0.6 else "탈락"
        years = len(bt.turnover.loc["2006":]) / 252
        rows.append({"전략": name, "판정": verdict,
                     "기본설정 구간별 통과": " / ".join("O" if x else "X" for x in base_pass),
                     "근처 설정 통과": f"{sum(grid_pass)}/{len(grid_pass)}",
                     "카나리아와 월 상관": f"{corr:.2f}",
                     "혼합 후보": "예" if all(mix_ok) else "아니오",
                     "연 회전율": f"{bt.turnover.loc['2006':].sum() / years:.1f}배"})
        lines = [f"### {name}\n", f"{desc}. 기본 설정: {base}\n",
                 "| 구간 | 전략 연수익 | 전략 낙폭 | 전략 샤프 | SPY 연수익 | SPY 낙폭 | SPY 샤프 | 카나리아 연수익 | 카나리아 낙폭 | 카나리아 샤프 | 50:50 연수익 | 50:50 낙폭 | 50:50 샤프 |",
                 "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for label, p in {**PERIODS, "2006~ 전체": ("2006-01-01", None)}.items():
            vals = [stats(ac.per(x, p), irx) for x in (r, spy, canary, mix)]
            cells = " | ".join(f"{pct(a)} | {pct(b)} | {s:.2f}" for a, b, s in vals)
            lines.append(f"| {label} | {cells} |")
        cg25, _, _ = stats(ac.per(r, ("2006-01-01", None)), irx)
        cg10, _, _ = stats(ac.per(r10, ("2006-01-01", None)), irx)
        lines.append(f"\n비용 25bp 대신 10bp면 2006~ 연수익 {pct(cg25)} → {pct(cg10)}.\n")
        detail += lines

    out += ["## 요약\n", pd.DataFrame(rows).to_markdown(index=False), "",
            "'구간별 통과'는 2006~07 / 2008~14 / 2015~ 순서.\n", "## 계열별 자세히\n", *detail]
    Path("reports").mkdir(exist_ok=True)
    Path("reports/explore.md").write_text("\n".join(out), encoding="utf-8")
    pd.DataFrame(returns).loc["2005-12-01":].round(6).to_csv("reports/explore_returns.csv")
    print("\n".join(out))


if __name__ == "__main__":
    main()
