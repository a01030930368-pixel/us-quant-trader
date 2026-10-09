"""Backtest every candidate strategy and write a ranking report.

Usage:
    python scripts/run_backtest.py                      # defaults
    python scripts/run_backtest.py --start 2005-01-01 --split 2018-01-01 --cost-bps 15
Outputs reports/ranking.csv and reports/ranking.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.data import add_leveraged, load_prices  # noqa: E402
from quant.ranking import evaluate, expand_grid, Config  # noqa: E402

UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "TLT", "IEF", "GLD", "SHY"]
RISK = ["SPY", "QQQ", "IWM", "EFA", "GLD"]
# Simulated 3x ETFs; trade them as UPRO / TQQQ.
LEVERAGED = {"SPY3X": "SPY", "QQQ3X": "QQQ"}


def candidate_configs() -> list[Config]:
    configs = [Config("buy_and_hold", {"asset": "SPY"}),
               Config("buy_and_hold", {"asset": "QQQ"}),
               Config("buy_and_hold", {"asset": "QQQ3X"})]
    configs += expand_grid("sma_trend", {"asset": "SPY", "safe": "IEF"},
                           {"window": [150, 175, 200, 225, 250]})
    configs += expand_grid("momentum_rotation", {"assets": RISK, "safe": "IEF"},
                           {"lookback": [63, 126, 189, 252], "top_n": [1, 2, 3]})
    # Aggressive: 3x ETFs, timed on the unleveraged index.
    configs += expand_grid("sma_trend", {"asset": "QQQ3X", "signal": "QQQ", "safe": "IEF"},
                           {"window": [150, 175, 200, 225, 250]})
    configs += expand_grid("sma_trend", {"asset": "SPY3X", "signal": "SPY", "safe": "IEF"},
                           {"window": [150, 175, 200, 225, 250]})
    configs += expand_grid("momentum_rotation",
                           {"assets": ["QQQ3X", "SPY3X", "GLD", "TLT"], "safe": "IEF"},
                           {"lookback": [63, 126, 189, 252], "top_n": [1, 2]})
    configs += expand_grid("rsi2_reversion", {"asset": "SPY"},
                           {"entry": [5, 10, 15], "exit_": [60, 70, 80]})
    return configs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2007-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--split", default="2018-01-01",
                    help="이 날짜 이전은 학습(IS), 이후는 검증(OOS)")
    ap.add_argument("--cost-bps", type=float, default=15.0,
                    help="편도 거래비용(수수료+슬리피지+환전), bp 단위")
    ap.add_argument("--max-mdd", type=float, default=-0.30)
    args = ap.parse_args()

    prices = load_prices(UNIVERSE, args.start, args.end).dropna()
    for name, base in LEVERAGED.items():
        prices = add_leveraged(prices, base, name)
    table = evaluate(prices, candidate_configs(), split=args.split,
                     cost_bps=args.cost_bps, max_mdd=args.max_mdd)

    out = Path("reports")
    out.mkdir(exist_ok=True)
    table.drop(columns="_params").to_csv(out / "ranking.csv", index=False)

    cols = ["label", "passed", "score", "oos_cagr", "oos_month_avg", "oos_month_win_rate",
            "oos_worst_month", "oos_sharpe", "oos_mdd", "oos_calmar", "is_cagr",
            "is_sharpe", "robustness", "total_trades"]
    view = table[cols].copy()
    for c in ["oos_cagr", "oos_month_avg", "oos_month_win_rate", "oos_worst_month",
              "oos_mdd", "is_cagr"]:
        view[c] = (view[c] * 100).round(1).astype(str) + "%"
    for c in ["score", "oos_sharpe", "oos_calmar", "is_sharpe", "robustness"]:
        view[c] = view[c].round(2)
    header = (f"# 전략 순위\n\n데이터 {prices.index[0].date()} ~ {prices.index[-1].date()}, "
              f"검증(OOS) 시작 {args.split}, 거래비용 편도 {args.cost_bps}bp\n\n"
              "순위는 OOS 기준입니다. IS가 OOS보다 훨씬 좋으면 과최적화 의심.\n"
              "SPY3X/QQQ3X는 3배 레버리지 ETF(UPRO/TQQQ)를 지수로부터 모의 계산한 값입니다.\n"
              "month_avg=월평균 수익률, month_win_rate=수익 난 달 비율, worst_month=최악의 달\n\n")
    bench = view[table["strategy"] == "buy_and_hold"]
    is_lev = table["label"].str.contains("3X") & (table["strategy"] != "buy_and_hold")
    aggressive = view[is_lev].sort_values("score", ascending=False)
    body = ("## 비교 기준 (단순 보유)\n\n" + bench.to_markdown(index=False)
            + "\n\n## 공격형 (3배 레버리지, MDD 필터와 무관하게 점수순)\n\n"
            + aggressive.to_markdown(index=False)
            + "\n\n## 전체 순위\n\n" + view.to_markdown(index=False))
    (out / "ranking.md").write_text(header + body, encoding="utf-8")
    print("비교 기준:")
    print(bench.to_string(index=False))
    print("\n공격형 상위 10개:")
    print(aggressive.head(10).to_string(index=False))
    print("\n상위 15개:")
    print(view.head(15).to_string(index=False))
    print(f"\n전체 결과: {out / 'ranking.md'}")


if __name__ == "__main__":
    main()
