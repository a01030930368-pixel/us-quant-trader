"""대시보드 확인용 가짜 모의투자 기록을 만든다.

실제 실행기가 남기는 파일과 같은 형식(docs/paper-format.md)으로
dashboard/sample/ 아래에 state.json, trades.jsonl, equity.jsonl을 쓴다.
가격은 무작위로 만든 값이라 실제 시장과 관계없다.

    python dashboard/make_sample.py
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "sample"
KST = timezone(timedelta(hours=9))

STABLE = ["SPY", "QQQ", "IWM", "EFA", "GLD"]
AGGRESSIVE = ["TQQQ", "UPRO", "GLD", "TLT"]
SAFE = "IEF"
LOOKBACKS = [21, 63, 126, 252]  # 1·3·6·12개월
FEE = 0.0025  # 키움 해외주식 온라인 수수료 0.25% 가정

# 연 기대수익, 연 변동성, 시작 가격
PARAMS = {
    "SPY": (0.10, 0.16, 520.0), "QQQ": (0.13, 0.21, 450.0), "IWM": (0.07, 0.22, 205.0),
    "EFA": (0.06, 0.15, 80.0), "GLD": (0.08, 0.14, 230.0), "TQQQ": (0.30, 0.62, 62.0),
    "UPRO": (0.25, 0.48, 78.0), "TLT": (0.02, 0.15, 90.0), "IEF": (0.03, 0.07, 94.0),
}


def business_days(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def simulate_prices(days: list[date], seed: int = 9) -> dict[str, list[float]]:
    rng = random.Random(seed)
    prices = {s: [p[2]] for s, p in PARAMS.items()}
    for _ in days[1:]:
        market = rng.gauss(0, 1)
        for s, (mu, vol, _) in PARAMS.items():
            beta = {"TQQQ": 3, "UPRO": 3, "QQQ": 1.2, "SPY": 1, "IWM": 1.1, "EFA": 0.8}.get(s, 0)
            shock = (beta * market * 0.6 + rng.gauss(0, 1) * 0.8) if beta else rng.gauss(0, 1)
            shock /= math.sqrt((beta * 0.6) ** 2 + 0.64) if beta else 1
            r = mu / 252 + vol / math.sqrt(252) * shock
            prices[s].append(round(prices[s][-1] * math.exp(r), 2))
    return prices


def month_ends(days: list[date]) -> set[int]:
    return {i for i in range(len(days) - 1) if days[i + 1].month != days[i].month}


def signal(prices, i):
    ret = {s: sum(prices[s][i] / prices[s][i - lb] - 1 for lb in LOOKBACKS) / len(LOOKBACKS) for s in PARAMS}
    targets: dict[str, dict] = {}

    def add(sym, w, sleeve):
        key = (sym, sleeve)
        targets.setdefault(key, {"symbol": sym, "weight": 0.0, "sleeve": sleeve})
        targets[key]["weight"] += w

    best = max(STABLE, key=lambda s: ret[s])
    add(best if ret[best] > 0 else SAFE, 0.5, "stable")
    for s in sorted(AGGRESSIVE, key=lambda s: ret[s], reverse=True)[:2]:
        add(s if ret[s] > 0 else SAFE, 0.25, "aggressive")

    picked = {(t["symbol"], t["sleeve"]) for t in targets.values()}
    scores = []
    for sleeve, universe in (("stable", STABLE), ("aggressive", AGGRESSIVE)):
        for s in sorted(universe, key=lambda s: ret[s], reverse=True):
            scores.append({"symbol": s, "sleeve": sleeve, "return": round(ret[s], 4),
                           "selected": (s, sleeve) in picked})
    return list(targets.values()), scores


def main() -> None:
    days = business_days(date(2025, 3, 3), date(2026, 10, 8))
    prices = simulate_prices(days)
    start_i = next(i for i, d in enumerate(days) if d >= date(2026, 4, 1))
    ends = month_ends(days)

    capital = 10_000.0
    cash = capital
    qty: dict[str, int] = {}
    cost: dict[str, float] = {}
    sleeve_of: dict[str, str] = {}
    trades, equity = [], []
    pending = None  # 월말 신호 -> 다음 거래일 주문
    targets: list[dict] = []
    scores: list[dict] = []
    last_rebalance = None
    order_no = 1000

    # 첫날은 직전 월말 신호로 진입
    pending = (*signal(prices, start_i - 1), days[start_i - 1])
    signal_date = None

    for i in range(start_i, len(days)):
        d = days[i]
        if pending:
            targets, scores, signal_date = pending
            pending = None
            last_rebalance = d
            px = {s: prices[s][i] for s in PARAMS}
            equity_now = cash + sum(q * px[s] for s, q in qty.items())
            want: dict[str, int] = {}
            for t in targets:
                want[t["symbol"]] = want.get(t["symbol"], 0) + int(equity_now * t["weight"] * (1 - FEE) // px[t["symbol"]])
                sleeve_of[t["symbol"]] = t["sleeve"]
            orders = []
            for s in set(qty) | set(want):
                diff = want.get(s, 0) - qty.get(s, 0)
                if diff:
                    orders.append((s, diff))
            orders.sort(key=lambda o: o[1])  # 매도 먼저
            t0 = datetime(d.year, d.month, d.day, 22, 30, 4, tzinfo=KST)
            for k, (s, diff) in enumerate(orders):
                price = round(px[s] * (1 + (0.0004 if diff > 0 else -0.0004)), 2)
                amount = abs(diff) * price
                fee = round(amount * FEE, 2)
                if diff > 0:
                    cash -= amount + fee
                    cost[s] = (cost.get(s, 0.0) * qty.get(s, 0) + amount) / (qty.get(s, 0) + diff)
                else:
                    cash += amount - fee
                qty[s] = qty.get(s, 0) + diff
                if qty[s] == 0:
                    qty.pop(s)
                    cost.pop(s, None)
                order_no += 1
                trades.append({
                    "time": (t0 + timedelta(seconds=7 * k)).isoformat(),
                    "date": d.isoformat(), "symbol": s,
                    "side": "buy" if diff > 0 else "sell", "qty": abs(diff),
                    "price": price, "fee": fee, "status": "filled",
                    "order_id": f"S{order_no}", "sleeve": sleeve_of.get(s),
                    "note": "월간 리밸런싱",
                })
        value = sum(q * prices[s][i] for s, q in qty.items())
        equity.append({"date": d.isoformat(), "equity": round(cash + value, 2),
                       "cash": round(cash, 2), "benchmark_close": prices["SPY"][i]})
        if i in ends:
            pending = (*signal(prices, i), d)

    # 다음 리밸런싱: 10월 마지막 거래일 신호 -> 11월 첫 거래일 주문
    oct_last = date(2026, 10, 30)
    state = {
        "schema_version": 1,
        "strategy": {
            "id": "momentum_blend_multi",
            "name": "혼합 50/50 멀티기간 모멘텀",
            "description": "안정 바구니 50%는 평균 모멘텀(1·3·6·12개월 수익률 평균) 1등, 공격 바구니 50%는 1·2등을 반씩. 마이너스면 IEF.",
            "sleeves": [
                {"id": "stable", "name": "안정 바구니", "weight": 0.5, "universe": STABLE},
                {"id": "aggressive", "name": "공격 바구니", "weight": 0.5, "universe": AGGRESSIVE},
            ],
            "safe_asset": SAFE,
        },
        "mode": "simulate",
        "base_currency": "USD",
        "benchmark": "SPY",
        "started_at": days[start_i].isoformat(),
        "initial_capital": capital,
        "updated_at": datetime(2026, 10, 8, 6, 10, tzinfo=KST).isoformat(),
        "cash": round(cash, 2),
        "positions": [
            {"symbol": s, "qty": q, "avg_price": round(cost[s], 2),
             "last_price": prices[s][-1], "sleeve": sleeve_of[s]}
            for s, q in sorted(qty.items(), key=lambda kv: -kv[1] * prices[kv[0]][-1])
        ],
        "targets": targets,
        "last_rebalance": last_rebalance.isoformat(),
        "next_rebalance": {"signal_date": oct_last.isoformat(), "order_date": "2026-11-02"},
        "signal": {"as_of": signal_date.isoformat(),
                   "lookback_days": max(LOOKBACKS), "lookbacks": LOOKBACKS, "scores": scores},
        "alerts": [
            {"level": "info", "message": "샘플 데이터입니다. 실제 실행기가 paper/ 폴더에 기록을 남기면 그 값으로 바뀝니다."},
        ],
    }

    OUT.mkdir(exist_ok=True)
    (OUT / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUT / "trades.jsonl", "w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    with open(OUT / "equity.jsonl", "w", encoding="utf-8") as f:
        for e in equity:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"샘플 기록 작성: {OUT} (거래 {len(trades)}건, {len(equity)}일)")


if __name__ == "__main__":
    main()
