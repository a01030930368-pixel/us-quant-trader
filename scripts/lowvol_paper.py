"""저변동성 업종 모의 장부: 매일 밤 GitHub Actions에서 돌린다.

    python scripts/lowvol_paper.py --ledger paper_lowvol

키움 계좌와는 완전히 따로인 가상 계좌다(주문을 내지 않는다). 9개 업종 ETF
(XLB XLE XLF XLI XLK XLP XLU XLV XLY) 중 최근 6개월 변동성이 가장 낮은 3개를
균등 보유하는 규칙(quant.new_strategies.low_vol_sectors, 4분할 월 1회)을 2024년부터
다시 계산해서, 장부에 아직 없는 날의 수익률만 이어 붙인다. 이미 적힌 날은 고치지 않는다.

- 시작: 2026-10-09 종가에 $100,000 (4종 혼합 가상 계좌, 카나리아 모의투자와 같은 날)
- 체결은 다음 날 시가, 편도 0.25% 비용 (백테스트와 같은 조건)
- Mintory가 받아 카나리아 실제 기록과 70:30으로 섞은 선도 함께 그린다.

장부 형식은 4종 혼합과 같다: equity.jsonl 한 줄 = {"date", "equity", "ret", "sleeves": {}},
state.json = 지금 보유 업종과 요약.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.stockblend_paper import BASE_DATE, BASE_EQUITY, extend, last_complete_session, read_rows  # noqa: E402

ENGINE_START = "2024-01-02"
COST_BPS = 25.0
NAME = "저변동성 업종 (업종 ETF 3개)"
NAMES = {"XLB": "소재", "XLE": "에너지", "XLF": "금융", "XLI": "산업재", "XLK": "기술",
         "XLP": "필수소비재", "XLU": "유틸리티", "XLV": "헬스케어", "XLY": "임의소비재"}


def run_lowvol(close: pd.DataFrame, opens: pd.DataFrame) -> dict:
    """Daily net returns of the low-volatility sector strategy and today's target weights."""
    from quant.backtest import run_backtest
    from quant.new_strategies import low_vol_sectors

    w = low_vol_sectors(close)
    bt = run_backtest(close, w, COST_BPS, opens)
    ret = bt.returns.loc[ENGINE_START:]
    vol = close.pct_change().rolling(126).std().iloc[-1] * (252 ** 0.5)
    target = w.iloc[-1]
    return {"ret": ret, "sleeves": pd.DataFrame(index=ret.index),
            "holdings": [{"ticker": t, "name": NAMES.get(t, t), "weight": round(float(target[t]), 4),
                          "vol": round(float(vol[t]), 4)} for t in target.index if target[t] > 1e-9],
            "last_bar": close.index[-1]}


def main(argv: list[str] | None = None) -> int:
    from quant.data import load_prices
    from quant.new_strategies import SECTORS

    ap = argparse.ArgumentParser(description="저변동성 업종 모의 장부 갱신")
    ap.add_argument("--ledger", default="paper_lowvol", help="장부 폴더")
    args = ap.parse_args(argv)
    ledger = Path(args.ledger)
    ledger.mkdir(parents=True, exist_ok=True)
    eq_path = ledger / "equity.jsonl"

    now = datetime.now(timezone.utc)
    upto = last_complete_session(now)
    close = load_prices(SECTORS, "2022-06-01", use_cache=False).loc[:upto]
    opens = load_prices(SECTORS, "2022-06-01", use_cache=False, field="Open").reindex(close.index)
    out = run_lowvol(close, opens)
    rows = read_rows(eq_path)
    new = extend(rows, out["ret"], out["sleeves"].reindex(out["ret"].index), upto)
    with eq_path.open("a", encoding="utf-8") as f:
        for row in new:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    rows += new
    eq = pd.Series([r["equity"] for r in rows])
    state = {
        "schema_version": 1, "name": NAME, "updated_at": now.isoformat(timespec="seconds"),
        "base_date": BASE_DATE, "base_equity": BASE_EQUITY, "last_date": rows[-1]["date"],
        "equity": rows[-1]["equity"], "return": round(rows[-1]["equity"] / BASE_EQUITY - 1, 6),
        "max_drawdown": round(float((eq / eq.cummax() - 1).min()), 6), "cost_bps": COST_BPS,
        "mix_share": 0.3,
        "note": "가상 계좌입니다(주문 없음). 카나리아에 30% 섞는 후보라서 Mintory가 70:30 선도 함께 그립니다.",
        "holdings": out["holdings"],
    }
    (ledger / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"장부 {len(new)}줄 추가, 마지막 {rows[-1]['date']} ${rows[-1]['equity']:,.0f} "
          f"(시작 대비 {state['return'] * 100:+.2f}%), 보유 {', '.join(h['ticker'] for h in out['holdings'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
