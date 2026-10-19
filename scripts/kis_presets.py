"""Backtest the 10 preset strategies from Korea Investment's official repo
(koreainvestment/open-trading-api, backtester/kis_backtest/strategies/preset)
on US ETFs, next to our canary strategy and buy-and-hold.

The repo publishes the rules but no results. Rules and default parameters
follow its Python presets (which simplify some YAML descriptions, e.g.
"N days in a row" is coded as "up today"). Each preset trades one ETF,
all-in or all-cash:
  - the signal is read at day t's close and the position earns day t+1 on;
  - stop loss / take profit are checked on closes against the entry close;
  - "N-day high" means the high of the previous N closes (today excluded);
  - cost: 10bp one way on every entry and exit.

Usage: python scripts/kis_presets.py   (writes reports/kis_presets.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.data import load_prices  # noqa: E402
from quant.metrics import cagr, max_drawdown, sharpe  # noqa: E402

COST_BPS = 10.0
START = "2009-01-01"
FROM = "2011-02-14"  # same window as the canary backtest (real 3x ETFs exist)
SYMBOLS = ["SPY", "QQQ"]


def _cross_above(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_below(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def presets(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series) -> dict[str, tuple]:
    """name -> (entry, exit, stop_loss_pct, take_profit_pct) with KIS defaults."""
    roc1 = c.pct_change() * 100
    sma = lambda n: c.rolling(n).mean()  # noqa: E731
    prev_max = lambda n: c.shift(1).rolling(n).max()  # noqa: E731
    rng = (h - l).replace(0, float("nan"))
    ibs = ((c - l) / rng).fillna(0.5)
    roc60 = c.pct_change(60) * 100
    return {
        "골든/데드 크로스 (5·20일선)": (_cross_above(sma(5), sma(20)), _cross_below(sma(5), sma(20)), 5, 10),
        "52주 신고가 돌파": (_cross_above(c, prev_max(252)), pd.Series(False, c.index), 5, 15),
        "모멘텀 (60일 수익률 > 0)": (roc60 > 0, roc60 < 0, 10, None),
        "추세 필터 + 시그널 (60일선)": ((c > sma(60)) & (roc1 > 0), (c < sma(60)) & (roc1 < 0), 5, 10),
        "이동평균 이격도 (20일선 ±10%)": (c < sma(20) * 0.9, c > sma(20) * 1.1, None, None),
        "단기 반전 (5일선 ±3%)": (c < sma(5) * 0.97, c > sma(5) * 1.03, 5, None),
        "연속 상승/하락 (당일 등락)": (roc1 > 0, roc1 < 0, 5, None),
        "추세 돌파 후 이탈 (20일 고점)": (_cross_above(c, prev_max(20)), c < prev_max(20), 3, None),
        "강한 종가 (IBS 0.8)": (ibs >= 0.8, ibs < 0.2, 5, None),
        "변동성 돌파 (당일 +3%)": (roc1 > 3, roc1 < -3, 5, None),
    }


def simulate(c: pd.Series, entry: pd.Series, exit_: pd.Series, sl: float | None, tp: float | None,
             cost_bps: float = COST_BPS) -> tuple[pd.Series, pd.Series, int]:
    """Daily returns of an all-in / all-cash rule, its daily position, and the number of entries."""
    pos = pd.Series(0.0, c.index)
    holding, entry_px, trades = False, 0.0, 0
    for i, d in enumerate(c.index):
        px = c.iloc[i]
        if holding:
            chg = (px / entry_px - 1) * 100
            if exit_.iloc[i] or (sl and chg <= -sl) or (tp and chg >= tp):
                holding = False
        elif entry.iloc[i]:
            holding, entry_px, trades = True, px, trades + 1
        pos.iloc[i] = 1.0 if holding else 0.0
    ret = pos.shift(1).fillna(0.0) * c.pct_change().fillna(0.0)
    return ret - pos.diff().abs().fillna(pos.iloc[0]) * cost_bps / 10_000, pos, trades


def row(r: pd.Series, extra: dict | None = None) -> dict:
    r = r[r.index >= FROM]
    return {"연수익률": cagr(r), "최대낙폭": max_drawdown(r), "샤프": sharpe(r), **(extra or {})}


def main() -> None:
    from quant.report import backtest_returns
    fields = {f: load_prices(SYMBOLS, START, use_cache=False, field=f) for f in ("Close", "High", "Low", "Open")}
    rows = {}
    for sym in SYMBOLS:
        o, h, l, c = (fields[f][sym].dropna() for f in ("Open", "High", "Low", "Close"))
        for name, (en, ex, sl, tp) in presets(o, h, l, c).items():
            ret, pos, n = simulate(c, en.fillna(False), ex.fillna(False), sl, tp)
            years = (c.index[-1] - c.index[0]).days / 365.25
            rows[f"{sym} · {name}"] = row(ret, {"연간 매매": n / years, "투자 비중": pos[pos.index >= FROM].mean()})
        rows[f"{sym} · 그냥 사서 보유"] = row(c.pct_change().fillna(0.0), {"연간 매매": 0, "투자 비중": 1.0})
    canary, _, name = backtest_returns(cost_bps=COST_BPS)
    rows[f"우리 전략: {name}"] = row(canary, {"연간 매매": float("nan"), "투자 비중": 1.0})

    t = pd.DataFrame(rows).T
    fmt = t.copy()
    for col in ("연수익률", "최대낙폭", "투자 비중"):
        fmt[col] = fmt[col].map(lambda v: f"{v:.1%}")
    fmt["샤프"] = fmt["샤프"].map(lambda v: f"{v:.2f}")
    fmt["연간 매매"] = fmt["연간 매매"].map(lambda v: "-" if pd.isna(v) else f"{v:.0f}회")
    end = fields["Close"].index[-1]
    lines = [
        "# 한국투자증권 프리셋 전략 10종 백테스트 (미국 ETF)", "",
        f"기간 {FROM} ~ {end:%Y-%m-%d}, 수수료 편도 {COST_BPS / 100:.1f}%. 각 전략은 ETF 하나를 전부 사거나 전부 파는 방식, "
        "한국투자증권 저장소의 기본 설정값 그대로.", "",
        fmt.to_markdown(), "",
        "- 투자 비중: 기간 중 주식을 들고 있던 날의 비율. 나머지 날은 현금(이자 0으로 계산).",
        "- 손절·익절은 종가 기준으로 확인했어요(장중 가격은 반영 안 함).",
    ]
    out = Path("reports")
    out.mkdir(exist_ok=True)
    (out / "kis_presets.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
