"""4종 혼합(S&P 1500 개별주) 모의 장부: 매일 밤 GitHub Actions에서 돌린다.

    python scripts/stockblend_paper.py --ledger paper_stockblend

키움 계좌와는 완전히 따로인 가상 계좌다(주문을 내지 않는다). 매일 4종 혼합 규칙
(quant/stockblend, 다른 프로젝트에서 그대로 복사)을 2025년부터 다시 계산해서,
장부에 아직 없는 날의 수익률만 이어 붙인다. 이미 적힌 날은 절대 고치지 않는다.

- 시작: 2026-10-09 종가에 $100,000 (카나리아 모의투자를 바꾼 직후와 맞춤)
- 칸 4개(총이익률 상위 20, 실적 서프라이즈, 내부자 매수 상위 10, 주간 모멘텀) 25%씩,
  매달 초 다시 맞춤, 편도 0.25% 비용 (백테스트와 같은 조건)
- 종목 목록은 처음 실행한 날의 S&P 1500으로 고정한다(universe.txt).

장부 형식: equity.jsonl 한 줄 = {"date", "equity", "ret", "sleeves": {칸: 그날 수익률}},
state.json = 지금 칸별 보유 종목과 요약. Mintory가 이 파일들을 받아 카나리아와 비교한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
BASE_DATE = "2026-10-09"
BASE_EQUITY = 100_000.0
ENGINE_START = "2025-01-02"   # positions are already settled by the base date
COST_BPS = 25.0
NAME = "4종 혼합 (S&P 1500, 25%씩)"


def last_complete_session(now: datetime) -> pd.Timestamp:
    """The latest US session whose closing prices are final at `now`."""
    et = now.astimezone(ET)
    day = et.date() if (et.hour, et.minute) >= (16, 30) else et.date() - timedelta(days=1)
    return pd.Timestamp(day)


def extend(rows: list[dict], ret: pd.Series, sleeves: pd.DataFrame, upto: pd.Timestamp,
           base_date: str = BASE_DATE, base_equity: float = BASE_EQUITY) -> list[dict]:
    """New ledger rows for the days after the last recorded one, up to `upto`.

    Rows already written are never changed: each new day only chains its own
    return onto the last recorded balance.
    """
    new = []
    if not rows:
        new.append({"date": base_date, "equity": round(base_equity, 2), "ret": 0.0, "sleeves": {}})
    last = new[-1] if new else rows[-1]
    equity = float(last["equity"])
    for day, r in ret.loc[(ret.index > pd.Timestamp(last["date"])) & (ret.index <= upto)].items():
        equity *= 1 + float(r)
        new.append({"date": day.strftime("%Y-%m-%d"), "equity": round(equity, 2),
                    "ret": round(float(r), 6),
                    "sleeves": {k: round(float(v), 6) for k, v in sleeves.loc[day].items()}})
    return new


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_blend(universe_file: Path) -> dict:
    """Recompute the four sleeves and the blend from ENGINE_START."""
    from quant.stockblend import engine
    from quant.stockblend.blend import blend
    from quant.stockblend.data import load_ohlc
    from quant.stockblend.fundamentals import load_facts
    from quant.stockblend.insider import InsiderDollar, load_purchases, purchase_table
    from quant.stockblend.pead import PEAD, sue_table
    from quant.stockblend.quality import GrossProfitability, profitability_table
    from quant.stockblend.strategies import MomentumSwing
    from quant.stockblend.universe import MARKET, sp1500

    tickers = sp1500(cache=universe_file)
    warmup = str(pd.Timestamp(ENGINE_START).year - 1) + "-01-01"
    data = load_ohlc(tickers + [MARKET], start=warmup, use_cache=False)
    universe = [t for t in data["Close"].columns if t != MARKET]
    idx = data["Close"].index
    facts = load_facts(universe, use_cache=False)
    data["GPA"], gcovered = profitability_table(facts, idx)
    data["SUE"], scovered = sue_table(facts, idx)
    purchases = load_purchases(idx[0].year, idx[-1])
    data["IBUY"] = purchase_table(purchases, idx, universe)
    if gcovered == 0 or scovered == 0 or data["IBUY"].to_numpy().sum() == 0:
        raise RuntimeError("SEC 데이터를 받지 못했습니다. 장부를 건드리지 않고 멈춥니다.")

    runs, sleeves = {}, []
    for cls in (GrossProfitability, PEAD, InsiderDollar, MomentumSwing):
        strat = cls().prepare(data, market=MARKET)
        res = engine.run(strat, cost_bps=COST_BPS, start=ENGINE_START)
        runs[strat.label] = res.returns
        close = data["Close"].iloc[-1]
        sleeves.append({
            "name": strat.name, "label": strat.label, "weight": 0.25,
            "holdings": [{"ticker": p["ticker"], "entry_date": str(p["entry_date"]),
                          "entry_price": round(float(p["entry_price"]), 4),
                          "last_price": round(float(close.get(p["ticker"], float("nan"))), 4),
                          "return": round(float(close.get(p["ticker"], float("nan")) / p["entry_price"] - 1), 4)}
                         for p in res.open_positions],
        })
    ret = blend(runs, {k: 1.0 for k in runs}, COST_BPS)
    filed = purchases["filed"].max() if len(purchases) else None
    return {"ret": ret, "sleeves": pd.DataFrame(runs).fillna(0.0), "sleeve_state": sleeves,
            "universe": len(universe), "last_bar": idx[-1],
            "insider_latest": None if filed is None or pd.isna(filed) else filed.strftime("%Y-%m-%d")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="4종 혼합 모의 장부 갱신")
    ap.add_argument("--ledger", default="paper_stockblend", help="장부 폴더")
    args = ap.parse_args(argv)
    ledger = Path(args.ledger)
    ledger.mkdir(parents=True, exist_ok=True)
    eq_path = ledger / "equity.jsonl"

    now = datetime.now(timezone.utc)
    upto = last_complete_session(now)
    out = run_blend(ledger / "universe.txt")
    rows = read_rows(eq_path)
    new = extend(rows, out["ret"], out["sleeves"], upto)
    with eq_path.open("a", encoding="utf-8") as f:
        for row in new:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    rows += new
    eq = pd.Series([r["equity"] for r in rows])
    state = {
        "schema_version": 1, "name": NAME, "updated_at": now.isoformat(timespec="seconds"),
        "base_date": BASE_DATE, "base_equity": BASE_EQUITY, "last_date": rows[-1]["date"],
        "equity": rows[-1]["equity"], "return": round(rows[-1]["equity"] / BASE_EQUITY - 1, 6),
        "max_drawdown": round(float((eq / eq.cummax() - 1).min()), 6),
        "universe_size": out["universe"], "cost_bps": COST_BPS,
        "insider_latest_filing": out["insider_latest"],
        "note": ("가상 계좌입니다(주문 없음). 내부자 매수 칸은 SEC가 분기마다 내는 파일을 써서 "
                 "최근 몇 주~몇 달 매수가 늦게 반영됩니다."),
        "sleeves": out["sleeve_state"],
    }
    (ledger / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"장부 {len(new)}줄 추가, 마지막 {rows[-1]['date']} ${rows[-1]['equity']:,.0f} "
          f"(시작 대비 {state['return'] * 100:+.2f}%), 내부자 자료 {out['insider_latest']}까지")
    return 0


if __name__ == "__main__":
    sys.exit(main())
