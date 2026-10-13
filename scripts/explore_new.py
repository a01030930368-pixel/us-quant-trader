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
from quant.macro import load_fred  # noqa: E402
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
    c, o = add_tlt3x(c, o, irx)
    canary = ac.run(rc, ro, ac.PAPER)
    macro = {k: load_fred(k) for k in ("UNRATE", "BAA10Y", "DFII10", "T10Y3M")}
    return c, o, irx, high, low, vix, canary, macro


def add_tlt3x(c, o, irx, start="2009-05-01"):
    """3x long Treasuries: simulated before TMF existed (TMF expense ratio
    plus 2x the T-bill rate for the borrowed part), real TMF afterwards."""
    c, o = c.copy(), o.copy()
    daily = c["TLT"].pct_change().fillna(0.0) * 3 - (0.0106 + 2 * irx) / 252
    c["TLT3X"] = 100 * (1 + daily).cumprod()
    gap = (o["TLT"] / c["TLT"].shift(1) - 1).fillna(0.0)
    o["TLT3X"] = c["TLT3X"].shift(1).bfill() * (1 + 3 * gap)
    tc = load_prices(["TMF"], "2009-04-16").reindex(c.index)["TMF"]
    to = load_prices(["TMF"], "2009-04-16", field="Open").reindex(c.index)["TMF"]
    t0 = c.index[c.index >= pd.Timestamp(start)][0]
    scale = c.at[t0, "TLT3X"] / tc.at[t0]
    after = c.index >= t0
    c.loc[after, "TLT3X"] = tc[after] * scale
    o.loc[after, "TLT3X"] = to[after] * scale
    return c.ffill(), o.ffill()


def families(c, high, low, vix, macro):
    """name -> (description, base params, list of params, builder)."""
    v, v3 = vix["^VIX"], vix["^VIX3M"]
    return {
        "단기 반등 RSI(2)": (
            "많이 떨어진 날(RSI2 낮음) 상승추세면 사고, 반등하면 팖. 쉬는 돈은 현금",
            dict(asset="SPY", entry=10, trend=200),
            [dict(asset=a, entry=e, trend=t) for a, e, t in itertools.product(["SPY", "QQQ"], [5, 10, 15], [100, 200])],
            lambda p: ns.rsi_dip(c, **p)),
        "단기 반등 IBS": (
            "그날 저가 근처에서 끝나면 사고, 고가 근처에서 끝나면 팖. 쉬는 돈은 현금",
            dict(asset="SPY", entry=0.2, exit_=0.7),
            [dict(asset=a, entry=e, exit_=x) for a, e, x in itertools.product(["SPY", "QQQ"], [0.1, 0.2, 0.3], [0.6, 0.7, 0.8])],
            lambda p: ns.ibs_dip(c, high, low, **p)),
        "월말·월초 효과": (
            "월말 며칠~월초 며칠만 SPY, 나머지는 현금",
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
        "경기+가격 추세 (GTT)": (
            "실업률이 12개월 평균보다 높고(경기 둔화) SPY가 10개월 평균 아래일 때만 IEF, 그 외엔 SPY. 월 1회. 실업률은 발표 시차 1개월 반영",
            dict(sma_months=10, ur_months=12),
            [dict(sma_months=a, ur_months=b) for a, b in itertools.product([8, 10, 12], [9, 12, 15])],
            lambda p: ns.growth_trend(c, macro["UNRATE"], **p)),
        "신용+가격 추세": (
            "회사채 금리차(Baa-10년물)가 1년 평균보다 높고 SPY가 200일 평균 아래일 때만 IEF, 그 외엔 SPY. 매일 판단",
            dict(spread_window=252, sma_days=200),
            [dict(spread_window=a, sma_days=b) for a, b in itertools.product([126, 252, 504], [150, 200, 250])],
            lambda p: ns.credit_trend(c, macro["BAA10Y"], **p)),
        "계절성 (5월엔 팔아라)": (
            "11월~4월은 SPY, 5월~10월은 IEF",
            dict(in_month=11, out_month=5),
            [dict(in_month=a, out_month=b) for a, b in itertools.product([10, 11, 12], [4, 5, 6])],
            lambda p: ns.halloween(c, **p)),
        "주식·채권 위험균형": (
            "SPY와 장기채(TLT)를 변동성 반비례로 섞음. 월 1회, 4분할",
            dict(assets=("SPY", "TLT"), vol_window=63),
            [dict(assets=a, vol_window=v) for a, v in itertools.product([("SPY", "TLT"), ("SPY", "IEF"), ("QQQ", "TLT")], [21, 63, 126])],
            lambda p: ns.risk_parity(c, **p)),
        "3배 주식+3배 채권 (HFEA)": (
            "UPRO 55% + TMF 45% 고정 (2009년 전 TMF, 2010년 전 UPRO는 모의)",
            dict(stock_share=0.55),
            [dict(stock_share=x) for x in (0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7)],
            lambda p: ns.leveraged_pair(c, **p)),
        "3배 주식+3배 채권, 변동성 비중": (
            "UPRO와 TMF를 변동성 반비례로 섞음. 월 1회, 4분할",
            dict(vol_window=63),
            [dict(vol_window=v) for v in (21, 42, 63, 126, 252)],
            lambda p: ns.leveraged_parity(c, **p)),
        "변동성 관리 SPY": (
            "최근 변동성이 낮으면 SPY 비중을 최대 2배(UPRO 섞어서)까지, 높으면 줄이고 현금. 월 1회",
            dict(target=0.15, window=21, max_lev=2.0),
            [dict(target=t, window=w, max_lev=m) for t, w, m in itertools.product([0.12, 0.15, 0.18], [21, 63], [1.5, 2.0])],
            lambda p: ns.vol_managed(c, **p)),
        "경기+가격 추세 (GTT), 나스닥 2배": (
            "GTT 신호는 그대로(나스닥 기준), 투자 중엔 QQQ 50% + TQQQ 50%(=2배), 위험 신호면 IEF",
            dict(asset="QQQ", sma_months=10, ur_months=12, hold={"QQQ": 0.5, "QQQ3X": 0.5}),
            [dict(asset=a, sma_months=m, ur_months=u, hold=h) for (a, h), m, u in itertools.product(
                [("QQQ", {"QQQ": 0.5, "QQQ3X": 0.5}), ("SPY", {"SPY": 0.5, "SPY3X": 0.5})], [8, 10, 12], [9, 12, 15])],
            lambda p: ns.growth_trend(c, macro["UNRATE"], **p)),
        "업종 단기 반전": (
            "지난 한 달 가장 많이 빠진 업종 몇 개를 균등 보유. 월 1회, 4분할",
            dict(top_n=3, lookback=21),
            [dict(top_n=n, lookback=lb) for n, lb in itertools.product([2, 3, 4], [5, 10, 21, 42])],
            lambda p: ns.sector_reversal(c, **p)),
        "아이비 포트폴리오 (Faber)": (
            "SPY·EFA·IEF·VNQ·DBC 5등분, 각자 10개월 평균 위일 때만 보유, 아니면 그 몫은 SHY. 월 1회, 4분할",
            dict(sma_months=10),
            [dict(sma_months=m) for m in (6, 8, 10, 12)] + [dict(sma_months=10, assets=["SPY", "EFA", "IEF", "VNQ", "GLD"])],
            lambda p: ns.ivy(c, **p)),
        "금 + 실질금리": (
            "10년 물가연동채 금리가 3개월 평균보다 낮아지는 중이면 금(GLD), 아니면 SHY. 월 1회, 4분할",
            dict(window=63),
            [dict(window=w) for w in (21, 42, 63, 126, 252)],
            lambda p: ns.gold_real_rate(c, macro["DFII10"], **p)),
        "저변동성 업종 + 레버리지 (BAB)": (
            "저변동성 업종 80% + UPRO 20% (주식 노출 약 1.4배). 월 1회, 4분할",
            dict(lev_share=0.2),
            [dict(lev_share=x, top_n=n) for x, n in itertools.product([0.1, 0.15, 0.2, 0.25, 0.3], [3, 4])],
            lambda p: ns.levered_low_vol(c, **p)),
        "금리차+가격 추세": (
            "최근 1년 안에 장단기 금리 역전(10년-3개월<0)이 있었고 SPY가 10개월 평균 아래면 IEF, 아니면 SPY. 월 1회",
            dict(memory_months=12, sma_months=10),
            [dict(memory_months=a, sma_months=b) for a, b in itertools.product([6, 12, 24], [8, 10, 12])],
            lambda p: ns.curve_trend(c, macro["T10Y3M"], **p)),
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


MIX_FAMILIES = ["저변동성 업종", "고정 배분", "여러 자산 추세추종", "주식·채권 위험균형", "경기+가격 추세 (GTT)"]


def mix_section(c, o, irx, canary, fams, returns):
    """Canary + one diversifier at several shares (daily rebalanced), and for
    the low-volatility sectors every nearby setting at a 30% share."""
    lines = ["## 카나리아에 섞으면 (정밀 검사)\n",
             "카나리아 비중을 줄이고 그만큼 새 전략을 섞었을 때. 매일 비중을 맞춘다고 가정(비용 미반영, 실제로는 월 1회면 충분).\n",
             "| 섞은 전략 | 비중 | 2006~07 연수익/낙폭/샤프 | 2008~14 | 2015~ | 세 구간 모두 카나리아보다 샤프↑ |",
             "|---|---|---|---|---|---|"]

    def cells(r):
        return [stats(ac.per(r, p), irx) for p in PERIODS.values()]
    base = cells(canary)
    lines.append("| 카나리아 단독 | 0% | " + " | ".join(f"{pct(a)} / {pct(b)} / {s:.2f}" for a, b, s in base) + " | - |")
    for name in MIX_FAMILIES:
        for share in (0.2, 0.3, 0.5):
            m = cells((1 - share) * canary + share * returns[name])
            ok = all(x[2] > y[2] and x[1] >= y[1] - MDD_SLACK for x, y in zip(m, base))
            lines.append(f"| {name} | {share:.0%} | " + " | ".join(f"{pct(a)} / {pct(b)} / {s:.2f}" for a, b, s in m)
                         + f" | {'예' if ok else '아니오'} |")
    desc, base_p, grid, build = fams["저변동성 업종"]
    n_ok = 0
    for p in grid:
        r = run_backtest(c, build(p), COST, o).returns
        m = cells(0.7 * canary + 0.3 * r)
        n_ok += all(x[2] > y[2] and x[1] >= y[1] - MDD_SLACK for x, y in zip(m, base))
    lines += ["", f"저변동성 업종 30% 섞기를 근처 설정 {len(grid)}개로 바꿔 봐도 세 구간 모두 카나리아보다 나은 경우: {n_ok}/{len(grid)}개.\n"]
    return lines


def pct(x):
    return f"{x * 100:.1f}%"


def main():
    c, o, irx, high, low, vix, canary, macro = load_all()
    spy = ac.fixed(c, o, {"SPY": 1.0})
    fams = families(c, high, low, vix, macro)
    vix_start = vix["^VIX3M"].first_valid_index()

    out = ["# 새 전략 계열 탐색 (지금까지 전부)\n",
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
        r0 = run_backtest(c, base_w, 0.0, o).returns
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
        cg0, _, _ = stats(ac.per(r0, ("2006-01-01", None)), irx)
        lines.append(f"\n2006~ 연수익: 비용 25bp {pct(cg25)} / 10bp {pct(cg10)} / 비용 없음 {pct(cg0)}.\n")
        detail += lines

    out += ["## 요약\n", pd.DataFrame(rows).to_markdown(index=False), "",
            "'구간별 통과'는 2006~07 / 2008~14 / 2015~ 순서.\n",
            *mix_section(c, o, irx, canary, fams, returns), "## 계열별 자세히\n", *detail]
    Path("reports").mkdir(exist_ok=True)
    Path("reports/explore.md").write_text("\n".join(out), encoding="utf-8")
    pd.DataFrame(returns).loc["2005-12-01":].round(6).to_csv("reports/explore_returns.csv")
    print("\n".join(out))


if __name__ == "__main__":
    main()
