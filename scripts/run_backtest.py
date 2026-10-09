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

from quant.data import load_prices  # noqa: E402
from quant.ranking import evaluate, expand_grid, Config  # noqa: E402

UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "TLT", "IEF", "GLD", "SHY"]
RISK = ["SPY", "QQQ", "IWM", "EFA", "GLD"]


def candidate_configs() -> list[Config]:
    configs = [Config("buy_and_hold", {"asset": "SPY"}),
               Config("buy_and_hold", {"asset": "QQQ"})]
    configs += expand_grid("sma_trend", {"asset": "SPY", "safe": "IEF"},
                           {"window": [150, 175, 200, 225, 250]})
    configs += expand_grid("momentum_rotation", {"assets": RISK, "safe": "IEF"},
                           {"lookback": [63, 126, 189, 252], "top_n": [1, 2, 3]})
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
    table = evaluate(prices, candidate_configs(), split=args.split,
                     cost_bps=args.cost_bps, max_mdd=args.max_mdd)

    out = Path("reports")
    out.mkdir(exist_ok=True)
    table.drop(columns="_params").to_csv(out / "ranking.csv", index=False)

    cols = ["label", "passed", "score", "oos_cagr", "oos_sharpe", "oos_mdd",
            "oos_calmar", "is_cagr", "is_sharpe", "robustness", "total_trades"]
    view = table[cols].copy()
    for c in ["oos_cagr", "oos_mdd", "is_cagr"]:
        view[c] = (view[c] * 100).round(1).astype(str) + "%"
    for c in ["score", "oos_sharpe", "oos_calmar", "is_sharpe", "robustness"]:
        view[c] = view[c].round(2)
    header = (f"# 전략 순위\n\n데이터 {prices.index[0].date()} ~ {prices.index[-1].date()}, "
              f"검증(OOS) 시작 {args.split}, 거래비용 편도 {args.cost_bps}bp\n\n"
              "순위는 OOS 기준입니다. IS가 OOS보다 훨씬 좋으면 과최적화 의심.\n\n")
    bench = view[table["strategy"] == "buy_and_hold"]
    body = ("## 비교 기준 (단순 보유)\n\n" + bench.to_markdown(index=False)
            + "\n\n## 전체 순위\n\n" + view.to_markdown(index=False))
    (out / "ranking.md").write_text(header + body, encoding="utf-8")
    print("비교 기준:")
    print(bench.to_string(index=False))
    print("\n상위 15개:")
    print(view.head(15).to_string(index=False))
    print(f"\n전체 결과: {out / 'ranking.md'}")


if __name__ == "__main__":
    main()
