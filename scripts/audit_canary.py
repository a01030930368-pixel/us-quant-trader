"""Independent audit of the paper strategy (EEM canary, 4 tranches, 50/50 blend).

Answers the questions an outside reviewer would ask, with numbers:
  1. Core metrics per period: CAGR, vol, Sharpe (vs T-bills), Sortino, MDD,
     worst year, longest time under water, the five deepest drawdowns.
  2. How the simulated 3x ETFs compare with the real TQQQ/UPRO (2010~), and
     what a financing cost tied to T-bill rates does before 2010.
  3. A period nobody looked at before: 2006-2007 (data used to start in 2007).
  4. Benchmarks in the same periods: SPY, QQQ, 60/40, equal-weight 4 assets;
     alpha/beta vs SPY with t-stats.
  5. Robustness grid: tranche spacing, lookbacks, aggressive share, safe asset,
     execution delay, costs; share of variants beating SPY on both CAGR and MDD.
  6. Where it is weak: yearly returns, SPY up/down months, rolling 3-year.
  7. Korean investor view: KRW returns and 22% capital-gains tax.

All runs: 4 tranches (month end, +5, +10, +15 trading days) unless stated,
next-day open fills, one-way cost 25bp unless stated.

Usage: python scripts/audit_canary.py   (writes reports/audit.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.backtest import run_backtest  # noqa: E402
from quant.data import load_prices  # noqa: E402
from quant.metrics import monthly_returns  # noqa: E402
from quant.strategies import REGISTRY  # noqa: E402

COST = 25.0
LB = [21, 63, 126, 252]
PAPER = {"strategy": "canary_gate", "canary": ["EEM"], "lookbacks": LB, "tranches": 4, "spacing": 5}
TICKERS = ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT", "IEF", "SHY", "EEM", "AGG"]
LEV = {"QQQ3X": "QQQ", "SPY3X": "SPY"}
ER = 0.0095  # TQQQ/UPRO expense ratio
UNSEEN = ("2006-01-01", "2007-12-31")
OOS = ("2008-01-01", "2014-12-31")
REAL = ("2015-01-01", None)


# ---------- data ----------

def load():
    close = load_prices(TICKERS, "2004-01-01").dropna()
    opens = load_prices(TICKERS, "2004-01-01", field="Open").reindex(close.index)
    irx = load_prices(["^IRX"], "2004-01-01")["^IRX"].reindex(close.index).ffill().bfill() / 100
    fx = load_prices(["KRW=X"], "2004-01-01")["KRW=X"]
    fx = fx.where(fx.pct_change().abs() < 0.08).reindex(close.index).ffill().bfill()  # drop Yahoo spikes
    real_c = load_prices(["TQQQ", "UPRO"], "2010-02-11")
    real_o = load_prices(["TQQQ", "UPRO"], "2010-02-11", field="Open")
    return close, opens, irx, fx, real_c, real_o


def simulate_3x(close, opens, irx, financing: str):
    """Add QQQ3X/SPY3X. financing 'flat3' = 3%/yr (what earlier reports used),
    'tbill' = expense ratio + 2x the T-bill rate (the borrowed two-thirds)."""
    c, o = close.copy(), opens.copy()
    for lev, base in LEV.items():
        cost = 0.03 if financing == "flat3" else ER + 2 * irx
        daily = c[base].pct_change().fillna(0.0) * 3 - cost / 252
        c[lev] = 100 * (1 + daily).cumprod()
        gap = (o[base] / c[base].shift(1) - 1).fillna(0.0)
        o[lev] = c[lev].shift(1).bfill() * (1 + 3 * gap)
    return c, o


def splice_real(c, o, real_c, real_o, start="2010-03-01"):
    """Replace the simulated 3x columns with real TQQQ/UPRO from `start`,
    chaining their returns onto the simulated level so weights stay continuous."""
    c, o = c.copy(), o.copy()
    for lev, real in (("QQQ3X", "TQQQ"), ("SPY3X", "UPRO")):
        rc = real_c[real].reindex(c.index)
        ro = real_o[real].reindex(c.index)
        t0 = c.index[c.index >= pd.Timestamp(start)][0]
        scale = c.at[t0, lev] / rc.at[t0]
        after = c.index >= t0
        c.loc[after, lev] = rc[after] * scale
        o.loc[after, lev] = ro[after] * scale
    return c.ffill(), o.ffill()


# ---------- metrics ----------

def dd_table(r: pd.Series, n=5) -> pd.DataFrame:
    eq = (1 + r).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1
    rows, i = [], 0
    under = dd < 0
    starts = under & ~under.shift(1, fill_value=False)
    for s in dd.index[starts]:
        seg = dd.loc[s:]
        end = seg.index[seg >= 0]
        e = end[0] if len(end) else None
        part = dd.loc[s:e] if e is not None else seg
        rows.append({"시작": str(dd.index[dd.index.get_loc(s) - 1].date()) if dd.index.get_loc(s) else str(s.date()),
                     "바닥": str(part.idxmin().date()), "낙폭": float(part.min()),
                     "회복": str(e.date()) if e is not None else "아직",
                     "고점 회복까지(개월)": round(len(part) / 21, 1)})
        i += 1
    out = pd.DataFrame(rows).sort_values("낙폭").head(n)
    return out


def longest_under(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    under = (eq < eq.cummax()).values
    best = run = 0
    for f in under:
        run = run + 1 if f else 0
        best = max(best, run)
    return round(best / 21, 1)


def metrics(r: pd.Series, rf: pd.Series) -> dict:
    r = r.dropna()
    years = len(r) / 252
    tot = float((1 + r).prod())
    cagr = tot ** (1 / years) - 1
    ex = r - rf.reindex(r.index).fillna(0) / 252
    vol = float(r.std() * np.sqrt(252))
    sharpe = float(ex.mean() / ex.std() * np.sqrt(252))
    down = ex.clip(upper=0)
    sortino = float(ex.mean() * 252 / (np.sqrt((down ** 2).mean()) * np.sqrt(252)))
    eq = (1 + r).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    yr = (1 + r).groupby(r.index.year).prod() - 1
    full_years = yr[[y for y in yr.index if (r.index.year == y).sum() > 240]]
    m = monthly_returns(r)
    return {"연수익(CAGR)": cagr, "변동성": vol, "샤프": sharpe, "소르티노": sortino, "최대낙폭": mdd,
            "칼마": cagr / abs(mdd) if mdd else np.nan,
            "최악의 해": float(full_years.min()) if len(full_years) else np.nan,
            "최악의 해 연도": str(full_years.idxmin()) if len(full_years) else "-",
            "최악의 달": float(m.min()), "최장 손실기간(개월)": longest_under(r)}


def per(r, period):
    a, b = period
    return r.loc[a:b] if b else r.loc[a:]


def fmt(df: pd.DataFrame) -> str:
    df = df.copy()
    for c in df.columns:
        if c in ("샤프", "소르티노", "칼마", "베타", "알파 t값", "t값") or "(개월)" in c:
            df[c] = df[c].map(lambda v: "-" if pd.isna(v) else f"{v:.2f}")
        elif df[c].dtype.kind == "f":
            df[c] = df[c].map(lambda v: "-" if pd.isna(v) else f"{v * 100:.1f}%")
    return df.to_markdown(index=False)


def alpha_beta(r: pd.Series, bench: pd.Series, rf: pd.Series) -> dict:
    """Monthly CAPM regression of excess returns on the benchmark's excess returns."""
    rfm = (1 + rf.reindex(r.index).fillna(0) / 252).groupby(r.index.to_period("M")).prod() - 1
    y = monthly_returns(r) - rfm
    x = monthly_returns(bench.reindex(r.index).fillna(0)) - rfm
    X = np.column_stack([np.ones(len(x)), x.values])
    coef, *_ = np.linalg.lstsq(X, y.values, rcond=None)
    resid = y.values - X @ coef
    s2 = resid @ resid / (len(y) - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    return {"연 알파": (1 + coef[0]) ** 12 - 1, "베타": coef[1], "알파 t값": coef[0] / se[0]}


# ---------- runs ----------

def weights(c, params):
    return REGISTRY["tranched"](c, **params)


def run(c, o, params, cost=COST, delay=0):
    w = weights(c, params)
    if delay:
        w = w.shift(delay)
    return run_backtest(c, w, cost, o).returns


def fixed(c, o, mix: dict, cost=COST):
    """Fixed mix (e.g. 60/40) held at its target weights every day; buy-and-hold
    when one asset. Rebalancing costs are not charged (generous to the benchmark)."""
    w = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    for k, v in mix.items():
        w[k] = v
    return run_backtest(c, w, cost, o).returns


def main():
    close, opens, irx, fx, real_c, real_o = load()
    sim3, simo3 = simulate_3x(close, opens, irx, "flat3")
    simT, simoT = simulate_3x(close, opens, irx, "tbill")
    # Chain used for headline numbers: T-bill financing before 2010-03, real ETFs after.
    rc, ro = splice_real(simT, simoT, real_c, real_o)
    out = ["# EEM 카나리아 전략 독립 검증\n",
           f"데이터 {close.index[0].date()} ~ {close.index[-1].date()} (Yahoo 수정주가, 배당 재투자). "
           "모두 4분할, 신호는 그날 종가, 체결은 다음 날 시가, 편도 비용 25bp(수수료+슬리피지+환전 스프레드 가정). "
           "3배 ETF는 2010-03 전 모의(보수 0.95% + 단기국채 금리 x2 차입비용), 이후 실제 TQQQ/UPRO.\n",
           f"구간: 처음 보는 구간 {UNSEEN[0][:4]}~{UNSEEN[1][:4]} / 2008~2014(모의 3배, 앞서 'OOS'라 부른 구간) / "
           "2015~(실제 3배, 설정을 고른 구간).\n"]

    # 1. Simulated 3x vs real
    rows = []
    for real, lev in (("TQQQ", "QQQ3X"), ("UPRO", "SPY3X")):
        rr = real_c[real].pct_change().loc["2010-03-01":].dropna()
        for name, c in (("3%/년 (기존 보고서)", sim3), ("보수+금리 연동 (이번)", simT)):
            sr = c[lev].pct_change().reindex(rr.index)
            yrs = len(rr) / 252
            rows.append({"ETF": real, "모의 방식": name,
                         "실제 연수익": float((1 + rr).prod() ** (1 / yrs) - 1),
                         "모의 연수익": float((1 + sr).prod() ** (1 / yrs) - 1),
                         "일별 상관": float(rr.corr(sr)),
                         "연 추적오차": float((sr - rr).std() * np.sqrt(252))})
    df = pd.DataFrame(rows)
    df["일별 상관"] = df["일별 상관"].map(lambda v: f"{v:.4f}")
    out += ["## 1. 모의 3배 ETF가 실제와 얼마나 같은가 (2010-03~)\n", fmt(df), ""]
    fin = []
    for name, (c, o) in (("3%/년", (sim3, simo3)), ("보수+금리 연동", (simT, simoT))):
        r = run(c, o, PAPER)
        for label, p in (("2006~2007", UNSEEN), ("2008~2014", OOS)):
            m = metrics(per(r, p), irx)
            fin.append({"모의 방식": name, "구간": label, "연수익(CAGR)": m["연수익(CAGR)"], "최대낙폭": m["최대낙폭"]})
    out += ["2010년 전 차입비용 가정에 따라 전략 결과가 얼마나 바뀌나:\n", fmt(pd.DataFrame(fin)), ""]

    # 2. Core metrics and benchmarks
    strat = run(rc, ro, PAPER)
    bench = {
        "SPY 보유": fixed(rc, ro, {"SPY": 1.0}),
        "QQQ 보유": fixed(rc, ro, {"QQQ": 1.0}),
        "60/40 (SPY/IEF)": fixed(rc, ro, {"SPY": 0.6, "IEF": 0.4}),
        "4자산 균등 (SPY/EFA/IEF/GLD)": fixed(rc, ro, {"SPY": .25, "EFA": .25, "IEF": .25, "GLD": .25}),
        "카나리아 없이 같은 전략": run(rc, ro, {k: v for k, v in PAPER.items() if k not in ("strategy", "canary")}),
    }
    for label, p in (("처음 보는 구간 2006~2007", UNSEEN), ("2008~2014", OOS),
                     ("2015~ (실제 3배)", REAL), ("2006~ 전체", (UNSEEN[0], None))):
        rows = []
        for name, r in [("**EEM 카나리아 (모의투자 전략)**", strat), *bench.items()]:
            m = metrics(per(r, p), irx)
            ab = alpha_beta(per(r, p), per(bench["SPY 보유"], p), irx) if name != "SPY 보유" else {}
            rows.append({"전략": name, **{k: v for k, v in m.items() if k != "최악의 해 연도"},
                         "최악의 해 연도": m["최악의 해 연도"], **ab})
        out += [f"## 2. 핵심 지표와 벤치마크: {label}\n",
                "샤프·소르티노는 단기국채 금리를 뺀 초과수익 기준. 알파·베타는 SPY 대비 월간 회귀.\n",
                fmt(pd.DataFrame(rows)), ""]

    full = per(strat, (UNSEEN[0], None))
    out += ["## 3. 가장 깊었던 낙폭 5개 (전체 구간)\n", fmt(dd_table(full)), ""]

    # 4. Robustness grid on 2008~2014 and 2015~
    spy = bench["SPY 보유"]
    grid = []
    variants = {"기준 (모의투자 설정)": (PAPER, COST, 0)}
    for sp in (3, 7, 10):
        variants[f"분할 간격 {sp}거래일"] = ({**PAPER, "spacing": sp}, COST, 0)
    for n in (1, 2, 8):
        variants[f"{n}분할"] = ({**PAPER, "tranches": n, "spacing": max(1, 20 // n)}, COST, 0)
    for off in (0, 5, 10, 15):
        variants[f"분할 없이 월말+{off}일"] = ({**PAPER, "tranches": 1, "spacing": 0, "offset": off}, COST, 0)
    for lbs, lbl in (([63], "3개월"), ([126], "6개월"), ([252], "12개월"), ([63, 126, 252], "3·6·12개월")):
        variants[f"순위 기간 {lbl}"] = ({**PAPER, "lookbacks": lbs}, COST, 0)
    for sh in (0.3, 0.7):
        variants[f"공격 비중 {sh:.0%}"] = ({**PAPER, "aggr_share": sh}, COST, 0)
    variants["피난처 SHY"] = ({**PAPER, "safe": "SHY"}, COST, 0)
    variants["피난처 현금"] = ({**PAPER, "safe": None}, COST, 0)
    variants["카나리아 AGG 추가"] = ({**PAPER, "canary": ["EEM", "AGG"]}, COST, 0)
    variants["카나리아 EFA로 교체"] = ({**PAPER, "canary": ["EFA"]}, COST, 0)
    variants["체결 하루 늦게"] = (PAPER, COST, 1)
    variants["체결 사흘 늦게"] = (PAPER, COST, 3)
    variants["비용 50bp"] = (PAPER, 50.0, 0)
    for name, (p, cost, delay) in variants.items():
        p = dict(p)
        off = p.pop("offset", None)
        if off is not None:
            r = run_backtest(rc, REGISTRY["canary_gate"](rc, offset=off, **{k: v for k, v in p.items()
                             if k not in ("strategy", "tranches", "spacing")}), cost, ro).returns
        else:
            r = run(rc, ro, p, cost, delay)
        row = {"변형": name}
        for label, per_ in (("05~07", UNSEEN), ("08~14", OOS), ("15~", REAL)):
            m, s = metrics(per(r, per_), irx), metrics(per(spy, per_), irx)
            row[f"{label} 연수익"] = m["연수익(CAGR)"]
            row[f"{label} 낙폭"] = m["최대낙폭"]
            row[f"{label} SPY 이김"] = "예" if (m["연수익(CAGR)"] > s["연수익(CAGR)"]
                                              and m["최대낙폭"] > s["최대낙폭"]) else "아니오"
        grid.append(row)
    g = pd.DataFrame(grid)
    wins = {c: int((g[c] == "예").sum()) for c in g.columns if c.endswith("SPY 이김")}
    out += ["## 4. 설정을 바꿔도 유지되나\n",
            "'SPY 이김' = 같은 구간에서 연수익이 더 높고 최대낙폭이 더 얕음. "
            + ", ".join(f"{k.split()[0]}: {v}/{len(g)}개 이김" for k, v in wins.items()) + "\n",
            fmt(g), ""]

    # 5. Weaknesses
    yr = pd.DataFrame({"전략": (1 + full).groupby(full.index.year).prod() - 1,
                       "SPY": (1 + per(spy, (UNSEEN[0], None))).groupby(full.index.year).prod() - 1,
                       "QQQ": (1 + per(bench["QQQ 보유"], (UNSEEN[0], None))).groupby(full.index.year).prod() - 1})
    yr["SPY 대비"] = yr["전략"] - yr["SPY"]
    yr.index = yr.index.astype(str)
    yr.index.name = "연도"
    ms, mb = monthly_returns(full), monthly_returns(per(spy, (UNSEEN[0], None)))
    q = pd.qcut(mb, 5, labels=["SPY 하위 20% 달", "2", "3", "4", "SPY 상위 20% 달"])
    cond = pd.DataFrame({"SPY 평균": mb.groupby(q, observed=True).mean(),
                         "전략 평균": ms.groupby(q, observed=True).mean(),
                         "달 수": mb.groupby(q, observed=True).size().astype(float)}).reset_index()
    cond["달 수"] = cond["달 수"].astype(int)
    cond = cond.rename(columns={"index": "구분", mb.name or 0: "구분"})
    w = weights(rc, PAPER)
    off_share = (w["IEF"] > 0.99).loc[UNSEEN[0]:].mean()
    roll = ((1 + full).rolling(756).apply(np.prod, raw=True) ** (1 / 3) - 1).dropna()
    rspy = ((1 + per(spy, (UNSEEN[0], None))).rolling(756).apply(np.prod, raw=True) ** (1 / 3) - 1).dropna()
    out += ["## 5. 어디서 약한가\n", "### 연도별\n", fmt(yr.reset_index()), "",
            "### SPY가 크게 오른/내린 달에 전략은\n", fmt(cond), "",
            f"- 전 재산이 IEF(채권)에 있었던 날 비율: {off_share:.0%}\n"
            f"- 3년 굴린 연수익: 최악 {roll.min():.1%} ({roll.idxmin().date()}까지 3년), "
            f"SPY보다 높았던 3년 구간 비율 {(roll > rspy.reindex(roll.index)).mean():.0%}\n"
            f"- 12개월 최악 수익률: {((1 + full).rolling(252).apply(np.prod, raw=True) - 1).min():.1%}\n", ""]

    # 6. Korean investor: KRW and tax
    def krw(r):
        f = fx.reindex(r.index).ffill()
        return (1 + r) * (f / f.shift(1)).fillna(1.0) - 1

    def after_tax(r_krw, annual: bool, start=100_000_000, ded=2_500_000):
        v = start
        for y, g in (1 + r_krw).groupby(r_krw.index.year):
            v_end = v * float(g.prod())
            if annual:
                v_end -= 0.22 * max(0.0, v_end - v - ded)
            v = v_end
        if not annual:
            v -= 0.22 * max(0.0, v - start - ded)
        years = len(r_krw) / 252
        return (v / start) ** (1 / years) - 1

    rows = []
    for label, p in (("2008~", ("2008-01-01", None)), ("2015~", REAL)):
        for name, r, annual in (("EEM 카나리아 (매년 실현 가정)", strat, True), ("SPY 보유 (마지막에 한 번 매도)", spy, False),
                                ("QQQ 보유 (마지막에 한 번 매도)", bench["QQQ 보유"], False)):
            rr = per(r, p)
            rk = krw(rr)
            rows.append({"구간": label, "전략": name, "달러 연수익": metrics(rr, irx)["연수익(CAGR)"],
                         "원화 연수익": metrics(rk, irx)["연수익(CAGR)"],
                         "원화 최대낙폭": metrics(rk, irx)["최대낙폭"],
                         "원화 세후 연수익": after_tax(rk, annual)})
    out += ["## 6. 한국 투자자 기준 (원화 환산, 양도세 22%)\n",
            "원금 1억 원. 세금은 해마다 (평가이익 - 250만 원) x 22%로 단순화했습니다(손실 이월 없음). "
            "전략은 매달 사고팔아 이익이 대부분 그해 실현되므로 매년 과세, SPY·QQQ 보유는 마지막에 한 번 과세로 계산했습니다. "
            "배당 원천징수(15%)와 환전 수수료는 빠져 있습니다.\n",
            fmt(pd.DataFrame(rows)), ""]

    Path("reports").mkdir(exist_ok=True)
    text = "\n".join(out).replace("| nan ", "| -   ")
    Path("reports/audit.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
