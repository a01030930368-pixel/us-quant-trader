"""One run of the monthly trader. Safe to run every day.

What a run does:
  1. Load daily closes up to the latest completed session.
  2. The signal date is the last session of the previous calendar month.
     If that signal was already executed, only record a snapshot and stop.
  3. Otherwise compute target weights as of the signal date, size orders
     against current equity, sell first, then buy.
  4. Write state/status.json, state/trades.jsonl and state/equity.csv
     (the dashboard reads only these files; see docs/dashboard-data.md).

Running daily means a missed day (PC off) is simply executed on the next run.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from quant.data import load_prices
from quant.strategies import blend

# Strategy symbol -> tradable ticker.
TRADE_TICKER = {"QQQ3X": "TQQQ", "SPY3X": "UPRO"}
STABLE = ["SPY", "QQQ", "IWM", "EFA", "GLD"]
AGGR = ["QQQ3X", "SPY3X", "GLD", "TLT"]
DATA_TICKERS = sorted(set(STABLE + ["TLT", "IEF", "TQQQ", "UPRO"]))


@dataclass
class TraderConfig:
    aggr_share: float = 0.5
    lookback: int = 63
    cash_buffer: float = 0.02          # keep 2% in cash for fees / price moves
    max_order_usd: float = 50_000.0    # refuse any single order above this
    min_trade_usd: float = 50.0        # skip tiny rebalancing trades
    state_dir: Path = Path("state")
    strategy_name: str = field(init=False)

    def __post_init__(self):
        self.strategy_name = (f"혼합 {round((1 - self.aggr_share) * 100)}/"
                              f"{round(self.aggr_share * 100)} 모멘텀")


def target_weights(close: pd.DataFrame, cfg: TraderConfig) -> pd.DataFrame:
    """Strategy weights on strategy symbols (QQQ3X/SPY3X are the real
    TQQQ/UPRO closes here, renamed)."""
    px = close.rename(columns={v: k for k, v in TRADE_TICKER.items()})
    return blend(px, [
        ("momentum_rotation", {"assets": STABLE, "safe": "IEF",
                               "lookback": cfg.lookback, "top_n": 1}, 1 - cfg.aggr_share),
        ("momentum_rotation", {"assets": AGGR, "safe": "IEF",
                               "lookback": cfg.lookback, "top_n": 2}, cfg.aggr_share),
    ])


def signal_date(index: pd.DatetimeIndex, today: pd.Timestamp) -> pd.Timestamp | None:
    """Last session strictly before the current calendar month."""
    prior = index[index < today.normalize().replace(day=1)]
    return prior[-1] if len(prior) else None


def plan_orders(weights: dict[str, float], prices: dict[str, float],
                positions: dict[str, int], cash: float, cfg: TraderConfig) -> list[dict]:
    equity = cash + sum(q * prices[t] for t, q in positions.items())
    investable = equity * (1 - cfg.cash_buffer)
    tickers = set(weights) | set(positions)
    orders = []
    for t in sorted(tickers):
        target = math.floor(investable * weights.get(t, 0.0) / prices[t])
        diff = target - positions.get(t, 0)
        if diff == 0 or abs(diff) * prices[t] < cfg.min_trade_usd:
            continue
        orders.append({"ticker": t, "side": "buy" if diff > 0 else "sell",
                       "qty": abs(diff), "ref_price": prices[t],
                       "value": abs(diff) * prices[t], "target_qty": target})
    # Sells first so their proceeds fund the buys.
    return sorted(orders, key=lambda o: o["side"] != "sell")


class Runner:
    def __init__(self, broker, cfg: TraderConfig | None = None, mode: str = "sim"):
        self.broker, self.cfg, self.mode = broker, cfg or TraderConfig(), mode
        self.dir = Path(self.cfg.state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- state files --------------------------------------------------------
    def _meta(self) -> dict:
        p = self.dir / "meta.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _save_meta(self, meta: dict) -> None:
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _log_trade(self, rec: dict) -> None:
        with open(self.dir / "trades.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _snapshot(self, date: str, equity: float, cash: float) -> None:
        p = self.dir / "equity.csv"
        df = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["date", "equity", "cash"])
        df = df[df["date"] != date]
        df.loc[len(df)] = [date, round(equity, 2), round(cash, 2)]
        df.sort_values("date").to_csv(p, index=False)

    # -- main ---------------------------------------------------------------
    def run(self, close: pd.DataFrame | None = None, now: datetime | None = None,
            execute: bool = True) -> dict:
        now = now or datetime.now(timezone.utc)
        if (self.dir / "STOP").exists():
            return self._write_status(now, note="STOP 파일이 있어 매매를 멈췄습니다.", halted=True)

        if close is None:
            close = load_prices(DATA_TICKERS, "2009-01-01", use_cache=False).dropna()
        last = close.index[-1]
        prices = {t: float(close[t].iloc[-1]) for t in close.columns}

        sig = signal_date(close.index, pd.Timestamp(now.date()))
        meta = self._meta()
        due = sig is not None and meta.get("last_signal") != str(sig.date())

        weights_now = {}
        if sig is not None:
            w = target_weights(close.loc[:sig], self.cfg).loc[sig]
            weights_now = {TRADE_TICKER.get(k, k): float(v) for k, v in w.items() if v > 1e-9}

        positions, cash = self.broker.positions(), self.broker.cash_usd()
        orders, results = [], []
        if due:
            orders = plan_orders(weights_now, prices, positions, cash, self.cfg)
            too_big = [o for o in orders if o["value"] > self.cfg.max_order_usd]
            if too_big:
                return self._write_status(now, note=f"한도 초과 주문 차단: {too_big[0]['ticker']}",
                                          halted=True, weights=weights_now, orders=orders)
            if execute:
                for o in orders:
                    res = self.broker.place_order(o["ticker"], o["side"], o["qty"], o["ref_price"])
                    rec = {"time": now.isoformat(), "signal_date": str(sig.date()),
                           "mode": self.mode, **o, **res}
                    self._log_trade(rec)
                    results.append(rec)
                if all(r.get("ok") for r in results):
                    meta["last_signal"] = str(sig.date())
                    self._save_meta(meta)
                positions, cash = self.broker.positions(), self.broker.cash_usd()

        equity = cash + sum(q * prices.get(t, 0.0) for t, q in positions.items())
        self._snapshot(str(last.date()), equity, cash)
        note = ("리밸런싱 실행" if due and execute else
                "리밸런싱 대상(미실행: 점검 모드)" if due else "이번 달 리밸런싱 완료, 대기 중")
        return self._write_status(now, note=note, weights=weights_now, orders=orders,
                                  results=results, positions=positions, cash=cash,
                                  prices=prices, equity=equity, last_close=str(last.date()),
                                  signal=str(sig.date()) if sig is not None else None)

    def _write_status(self, now, note, halted=False, weights=None, orders=None, results=None,
                      positions=None, cash=None, prices=None, equity=None,
                      last_close=None, signal=None) -> dict:
        prices = prices or {}
        holdings = [{"ticker": t, "qty": q, "price": prices.get(t),
                     "value": round(q * prices[t], 2) if t in prices else None,
                     "weight": round(q * prices[t] / equity, 4) if equity and t in prices else None}
                    for t, q in (positions or {}).items()]
        status = {
            "schema": 1,
            "updated_at": now.isoformat(),
            "mode": self.mode,
            "strategy": self.cfg.strategy_name,
            "halted": halted,
            "note": note,
            "last_close": last_close,
            "signal_date": signal,
            "next_rebalance": "다음 달 첫 거래일",
            "equity_usd": round(equity, 2) if equity is not None else None,
            "cash_usd": round(cash, 2) if cash is not None else None,
            "holdings": holdings,
            "target_weights": {k: round(v, 4) for k, v in (weights or {}).items()},
            "planned_orders": orders or [],
            "order_results": results or [],
        }
        (self.dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
        return status
