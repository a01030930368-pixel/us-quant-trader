"""One run of the monthly trader. Safe to run every day.

What a run does:
  1. Load daily closes up to the latest completed session.
  2. The signal date is the last session of the previous calendar month.
     If that signal was already executed, only record a snapshot and stop.
  3. Otherwise compute target weights as of the signal date, size orders
     against current equity, sell first, then buy.
  4. Write paper/state.json, paper/trades.jsonl and paper/equity.jsonl in the
     format the dashboard reads (docs/paper-format.md).

Running daily means a missed day (PC off) is simply executed on the next run.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from quant.data import load_prices
from quant.strategies import momentum_rotation

KST = timezone(timedelta(hours=9))
SAFE = "IEF"
BENCHMARK = "SPY"


@dataclass
class Sleeve:
    id: str
    name: str
    universe: list[str]
    top_n: int


SLEEVES = [
    Sleeve("stable", "안정 바구니", ["SPY", "QQQ", "IWM", "EFA", "GLD"], 1),
    Sleeve("aggressive", "공격 바구니", ["TQQQ", "UPRO", "GLD", "TLT"], 2),
]
DATA_TICKERS = sorted({t for s in SLEEVES for t in s.universe} | {SAFE, BENCHMARK})


@dataclass
class TraderConfig:
    aggr_share: float = 0.5
    lookback: int = 63
    initial_capital: float = 10_000.0
    cash_buffer: float = 0.02          # keep 2% in cash for fees / price moves
    max_order_usd: float = 50_000.0    # refuse any single order above this
    min_trade_usd: float = 50.0        # skip tiny rebalancing trades
    max_data_age_days: int = 5         # refuse to trade on stale prices
    state_dir: Path = Path("paper")

    def sleeve_weight(self, sleeve: Sleeve) -> float:
        return self.aggr_share if sleeve.id == "aggressive" else 1 - self.aggr_share

    @property
    def strategy_id(self) -> str:
        return f"mixed_{round((1 - self.aggr_share) * 100)}_{round(self.aggr_share * 100)}"

    @property
    def strategy_name(self) -> str:
        return (f"혼합 {round((1 - self.aggr_share) * 100)}/"
                f"{round(self.aggr_share * 100)} 모멘텀")


def signal_date(index: pd.DatetimeIndex, today: pd.Timestamp) -> pd.Timestamp | None:
    """Last session strictly before the current calendar month."""
    prior = index[index < today.normalize().replace(day=1)]
    return prior[-1] if len(prior) else None


def sleeve_targets(close: pd.DataFrame, sig: pd.Timestamp, cfg: TraderConfig) -> list[dict]:
    """Target weights per sleeve as of `sig` (one row per sleeve/symbol)."""
    px = close.loc[:sig]
    rows = []
    for sl in SLEEVES:
        w = momentum_rotation(px, sl.universe, lookback=cfg.lookback,
                              top_n=sl.top_n, safe=SAFE).loc[sig]
        for sym, v in w.items():
            if v > 1e-9:
                rows.append({"symbol": sym, "weight": round(float(v) * cfg.sleeve_weight(sl), 6),
                             "sleeve": sl.id})
    return rows


def signal_scores(close: pd.DataFrame, sig: pd.Timestamp, cfg: TraderConfig,
                  targets: list[dict]) -> list[dict]:
    chosen = {(t["sleeve"], t["symbol"]) for t in targets}
    ret = close.loc[:sig].pct_change(cfg.lookback).loc[sig]
    return [{"symbol": sym, "sleeve": sl.id, "return": round(float(ret[sym]), 4),
             "selected": (sl.id, sym) in chosen}
            for sl in SLEEVES for sym in sl.universe]


def next_rebalance(today: pd.Timestamp) -> dict:
    """Approximate: last weekday of this month, then the next weekday
    (US holidays are not modelled)."""
    month_end = today + pd.offsets.BMonthEnd(0)
    return {"signal_date": str(month_end.date()),
            "order_date": str((month_end + pd.offsets.BDay(1)).date())}


def plan_orders(weights: dict[str, float], prices: dict[str, float],
                positions: dict[str, dict], cash: float, cfg: TraderConfig) -> list[dict]:
    equity = cash + sum(p["qty"] * prices[t] for t, p in positions.items())
    investable = equity * (1 - cfg.cash_buffer)
    orders = []
    for t in sorted(set(weights) | set(positions)):
        target = math.floor(investable * weights.get(t, 0.0) / prices[t])
        diff = target - positions.get(t, {}).get("qty", 0)
        if diff == 0 or abs(diff) * prices[t] < cfg.min_trade_usd:
            continue
        orders.append({"symbol": t, "side": "buy" if diff > 0 else "sell",
                       "qty": abs(diff), "ref_price": prices[t], "value": abs(diff) * prices[t]})
    return sorted(orders, key=lambda o: o["side"] != "sell")  # sells fund buys


class Runner:
    def __init__(self, broker, cfg: TraderConfig | None = None, mode: str = "simulate"):
        self.broker, self.cfg, self.mode = broker, cfg or TraderConfig(), mode
        self.dir = Path(self.cfg.state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- files ----------------------------------------------------------------
    def _read(self, name: str) -> dict:
        p = self.dir / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _write_atomic(self, name: str, data: dict) -> None:
        tmp = self.dir / f".{name}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / name)

    def _append(self, name: str, rec: dict) -> None:
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _equity_row(self, rec: dict) -> None:
        p = self.dir / "equity.jsonl"
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
        rows = [r for r in rows if r["date"] != rec["date"]] + [rec]
        rows.sort(key=lambda r: r["date"])
        tmp = self.dir / ".equity.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        os.replace(tmp, p)

    # -- main -----------------------------------------------------------------
    def run(self, close: pd.DataFrame | None = None, now: datetime | None = None,
            execute: bool = True) -> dict:
        now = (now or datetime.now(timezone.utc)).astimezone(KST)
        today = pd.Timestamp(now.date())
        meta = self._read("meta.json")
        alerts: list[dict] = []

        if (self.dir / "STOP").exists():
            alerts.append({"level": "error", "message": "STOP 파일이 있어 매매를 멈췄습니다."})
            return self._state(now, meta, alerts=alerts)

        if close is None:
            close = load_prices(DATA_TICKERS, "2009-01-01", use_cache=False).dropna()
        last = close.index[-1]
        prices = {t: float(close[t].iloc[-1]) for t in close.columns}
        age = (today - last.normalize()).days
        if age > self.cfg.max_data_age_days:
            alerts.append({"level": "error",
                           "message": f"가격 데이터가 {age}일 전 것이라 매매하지 않았습니다."})
            return self._state(now, meta, alerts=alerts, prices=prices)

        sig = signal_date(close.index, today)
        targets = sleeve_targets(close, sig, self.cfg) if sig is not None else []
        scores = signal_scores(close, sig, self.cfg, targets) if sig is not None else []
        weights: dict[str, float] = {}
        for t in targets:
            weights[t["symbol"]] = weights.get(t["symbol"], 0.0) + t["weight"]

        positions, cash = self.broker.positions(), self.broker.cash_usd()
        meta.setdefault("started_at", str(today.date()))
        meta.setdefault("initial_capital", cash + sum(p["qty"] * prices.get(s, 0.0)
                                                      for s, p in positions.items()))
        due = sig is not None and meta.get("last_signal") != str(sig.date())

        if due:
            orders = plan_orders(weights, prices, positions, cash, self.cfg)
            too_big = [o for o in orders if o["value"] > self.cfg.max_order_usd]
            if too_big:
                alerts.append({"level": "error", "message":
                               f"주문 한도 초과로 차단: {too_big[0]['symbol']} "
                               f"${too_big[0]['value']:,.0f}"})
            elif not execute:
                for o in orders:
                    alerts.append({"level": "info", "message":
                                   f"점검 모드 - {o['side']} {o['symbol']} {o['qty']}주 예정"})
            else:
                sleeve_of = {t["symbol"]: t["sleeve"] for t in targets}
                all_ok = True
                for o in orders:
                    res = self.broker.place_order(o["symbol"], o["side"], o["qty"], o["ref_price"])
                    ok = res.get("ok", False)
                    all_ok &= ok
                    rec = {"time": now.isoformat(timespec="seconds"), "date": str(last.date()),
                           "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                           "price": round(res.get("fill_price", o["ref_price"]), 4),
                           "fee": round(res.get("fee", 0.0), 2),
                           "status": res.get("status", "filled" if ok else "rejected"),
                           "order_id": res.get("order_no"),
                           "sleeve": sleeve_of.get(o["symbol"]), "note": "월간 리밸런싱"}
                    if not ok:
                        rec["message"] = res.get("msg", "")
                        alerts.append({"level": "warn", "message":
                                       f"{o['symbol']} {'매수' if o['side'] == 'buy' else '매도'} "
                                       f"주문이 거부되었습니다 ({res.get('msg', '')})"})
                    self._append("trades.jsonl", rec)
                if all_ok:
                    meta["last_signal"] = str(sig.date())
                    meta["last_rebalance"] = str(today.date())
                positions, cash = self.broker.positions(), self.broker.cash_usd()

        equity = cash + sum(p["qty"] * prices.get(s, 0.0) for s, p in positions.items())
        self._equity_row({"date": str(last.date()), "equity": round(equity, 2),
                          "cash": round(cash, 2),
                          "benchmark_close": round(prices.get(BENCHMARK, float("nan")), 4)})
        self._write_atomic("meta.json", meta)
        return self._state(now, meta, alerts=alerts, positions=positions, cash=cash,
                           prices=prices, targets=targets, scores=scores, sig=sig)

    def _state(self, now, meta, alerts, positions=None, cash=None, prices=None,
               targets=None, scores=None, sig=None) -> dict:
        prices = prices or {}
        if positions is None:
            try:
                positions, cash = self.broker.positions(), self.broker.cash_usd()
            except Exception:  # noqa: BLE001 - state must still be written
                positions, cash = {}, None
        sleeve_of = {t["symbol"]: t["sleeve"] for t in (targets or [])}
        state = {
            "schema_version": 1,
            "strategy": {
                "id": self.cfg.strategy_id,
                "name": self.cfg.strategy_name,
                "description": "안정 바구니는 3개월 수익률 1등, 공격 바구니는 1·2등을 반씩. "
                               "수익률이 마이너스면 IEF.",
                "sleeves": [{"id": s.id, "name": s.name, "weight": self.cfg.sleeve_weight(s),
                             "universe": s.universe} for s in SLEEVES],
                "safe_asset": SAFE,
            },
            "mode": self.mode,
            "base_currency": "USD",
            "benchmark": BENCHMARK,
            "started_at": meta.get("started_at"),
            "initial_capital": round(meta.get("initial_capital", self.cfg.initial_capital), 2),
            "updated_at": now.isoformat(timespec="seconds"),
            "cash": round(cash, 2) if cash is not None else None,
            "positions": [{"symbol": s, "qty": p["qty"], "avg_price": round(p["avg_price"], 4),
                           "last_price": prices.get(s), "sleeve": sleeve_of.get(s)}
                          for s, p in positions.items()],
            "targets": targets or [],
            "last_rebalance": meta.get("last_rebalance"),
            "next_rebalance": next_rebalance(pd.Timestamp(now.date())),
            "signal": {"as_of": str(sig.date()) if sig is not None else None,
                       "lookback_days": self.cfg.lookback, "scores": scores or []},
            "alerts": alerts,
        }
        self._write_atomic("state.json", state)
        return state
