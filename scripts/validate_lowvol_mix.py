"""Check the best mix found by explore_new.py the way it would really trade:
EEM canary + low-volatility sectors, combined into one set of target weights
(both sleeves 4 tranches, monthly), next-day open fills, 25bp one-way costs.

Usage: python scripts/validate_lowvol_mix.py   (writes reports/lowvol_mix.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant import new_strategies as ns  # noqa: E402
from quant.backtest import run_backtest  # noqa: E402
from scripts import audit_canary as ac  # noqa: E402
from scripts import explore_new as ex  # noqa: E402

PERIODS = {**ex.PERIODS, "2006~ 전체": ("2006-01-01", None)}


def main():
    c, o, irx, *_ = ex.load_all()
    can_w = ac.weights(c, ac.PAPER).reindex(columns=c.columns, fill_value=0.0)
    low_w = ns.low_vol_sectors(c)
    runs = {}
    for share in (0.0, 0.2, 0.3, 0.4, 0.5):
        w = (1 - share) * can_w + share * low_w
        bt = run_backtest(c, w, ex.COST, o)
        runs[f"카나리아 {100 - share * 100:.0f} + 저변동성 업종 {share * 100:.0f}"] = bt
    runs["저변동성 업종 단독"] = run_backtest(c, low_w, ex.COST, o)
    spy = ac.fixed(c, o, {"SPY": 1.0})
    qqq = ac.fixed(c, o, {"QQQ": 1.0})

    out = ["# 카나리아 + 저변동성 업종: 실제 매매처럼 검증\n",
           "두 전략의 목표 비중을 합쳐 한 계좌로 운용한다고 보고 계산했습니다. 둘 다 4분할로 월 1회 맞추고, 다음 날 시가에 체결하며, 편도 비용은 25bp입니다. "
           "저변동성 업종은 9개 업종 ETF(XLB XLE XLF XLI XLK XLP XLU XLV XLY) 중 최근 6개월 변동성이 가장 낮은 3개를 균등하게 보유합니다.\n"]
    for label, p in PERIODS.items():
        rows = []
        for name, bt in runs.items():
            m = ac.metrics(ac.per(bt.returns, p), irx)
            years = max(len(ac.per(bt.turnover, p)) / 252, 1e-9)
            rows.append({"조합": name, **{k: v for k, v in m.items() if k != "최악의 해 연도"},
                         "최악의 해 연도": m["최악의 해 연도"], "연 회전율": ac.per(bt.turnover, p).sum() / years})
        for name, r in (("SPY 보유", spy), ("QQQ 보유", qqq)):
            m = ac.metrics(ac.per(r, p), irx)
            rows.append({"조합": name, **{k: v for k, v in m.items() if k != "최악의 해 연도"},
                         "최악의 해 연도": m["최악의 해 연도"], "연 회전율": float("nan")})
        df = pd.DataFrame(rows)
        df["연 회전율"] = df["연 회전율"].map(lambda v: "-" if pd.isna(v) else f"{v:.1f}배")
        out += [f"## {label}\n", ac.fmt(df), ""]

    full = {k: ac.per(v.returns, ("2006-01-01", None)) for k, v in runs.items()}
    yr = pd.DataFrame({k: (1 + r).groupby(r.index.year).prod() - 1 for k, r in full.items()
                       if k in ("카나리아 100 + 저변동성 업종 0", "카나리아 70 + 저변동성 업종 30", "저변동성 업종 단독")})
    yr["SPY"] = (1 + ac.per(spy, ("2006-01-01", None))).groupby(lambda d: d.year).prod() - 1
    yr.index = yr.index.astype(str)
    yr.index.name = "연도"
    out += ["## 연도별 (2006~)\n", ac.fmt(yr.reset_index()), "",
            "## 카나리아 70 + 저변동성 30의 가장 깊었던 낙폭 5개\n",
            ac.fmt(ac.dd_table(full["카나리아 70 + 저변동성 업종 30"])), ""]
    # Rolling 3-year windows: how often the mix had the better Sharpe / shallower drawdown.
    a, b = full["카나리아 100 + 저변동성 업종 0"], full["카나리아 70 + 저변동성 업종 30"]
    starts = a.index[: len(a) - 756: 21]
    better_sh = better_dd = better_ret = 0
    for st in starts:
        sa, sb = a.loc[st:].iloc[:756], b.loc[st:].iloc[:756]
        ma, mb = ac.metrics(sa, irx), ac.metrics(sb, irx)
        better_sh += mb["샤프"] > ma["샤프"]
        better_dd += mb["최대낙폭"] > ma["최대낙폭"]
        better_ret += mb["연수익(CAGR)"] > ma["연수익(CAGR)"]
    n = len(starts)
    out += ["## 3년씩 잘라 보면 (매달 시작점을 옮겨 가며)\n",
            f"카나리아 70 + 저변동성 30이 카나리아 단독보다 샤프가 높았던 3년 구간 {better_sh}/{n} ({better_sh / n:.0%}), "
            f"최대낙폭이 얕았던 구간 {better_dd}/{n} ({better_dd / n:.0%}), 연수익이 높았던 구간 {better_ret}/{n} ({better_ret / n:.0%}).\n"]
    Path("reports").mkdir(exist_ok=True)
    Path("reports/lowvol_mix.md").write_text("\n".join(out).replace("| nan ", "| -   "), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
