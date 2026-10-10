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
from quant.strategies import momentum_13612, momentum_blend, momentum_rotation, tranched

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
    lookback: int = 63                 # used only when lookbacks is empty
    # Rank on the average of these trailing returns (1/3/6/12 months), the
    # variant chosen in the strategy-improvement thread (momentum_blend).
    lookbacks: tuple[int, ...] = (21, 63, 126, 252)
    initial_capital: float = 10_000.0
    cash_buffer: float = 0.02          # keep 2% in cash for fees / price moves
    # Refuse any single order above this. None = current equity, which still
    # catches price/unit bugs; GLD sits in both sleeves, so one order can
    # legitimately be ~75% of the account (or 100% when everything goes to IEF).
    max_order_usd: float | None = None
    # One-way cost the backtest comparison assumes. None = 25bp on the Kiwoom
    # mock account (its fees run ~0.25%), 10bp elsewhere.
    model_cost_bps: float | None = None
    # Skip the day's orders when the account fell more than this fraction since
    # the previous recorded close (e.g. 0.10 = 10%). None turns it off.
    max_daily_loss: float | None = 0.10
    min_trade_usd: float = 50.0        # skip tiny rebalancing trades
    max_data_age_days: int = 5         # refuse to trade on stale prices
    state_dir: Path = Path("paper")
    # Canary + tranches (strategy thread, 2026-10-10). Empty canary and one
    # tranche = the plain monthly blend above. With canary=("EEM",) each
    # tranche moves to SAFE while EEM's 13612W momentum is <= 0; tranches=4
    # splits the money into parts that rebalance at month end, +5, +10 and
    # +15 trading days (quant.strategies.tranched / canary_gate).
    canary: tuple[str, ...] = ()
    tranches: int = 1
    tranche_spacing: int = 5

    @property
    def staggered(self) -> bool:
        return bool(self.canary) or self.tranches > 1

    def sleeve_weight(self, sleeve: Sleeve) -> float:
        return self.aggr_share if sleeve.id == "aggressive" else 1 - self.aggr_share

    @property
    def strategy_id(self) -> str:
        base = "momentum_blend_multi" if self.lookbacks else "momentum_blend"
        if self.canary:
            base += "_canary_" + "_".join(c.lower() for c in self.canary)
        if self.tranches > 1:
            base += f"_t{self.tranches}"
        return base if self.aggr_share == 0.5 else f"{base}_{round(self.aggr_share * 100)}"

    @property
    def strategy_name(self) -> str:
        split = f"{round((1 - self.aggr_share) * 100)}/{round(self.aggr_share * 100)}"
        name = f"혼합 {split} {'멀티기간 ' if self.lookbacks else ''}모멘텀"
        if self.tranches > 1:
            name = f"{self.tranches}분할 " + name
        if self.canary:
            name = f"{'·'.join(self.canary)} 카나리아 " + name
        return name

    def momentum(self, close: pd.DataFrame) -> pd.DataFrame:
        """Score used for ranking: mean trailing return over `lookbacks`."""
        windows = self.lookbacks or (self.lookback,)
        return sum(close.pct_change(lb) for lb in windows) / len(windows)


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
                              lookbacks=list(cfg.lookbacks) or None,
                              top_n=sl.top_n, safe=SAFE).loc[sig]
        for sym, v in w.items():
            if v > 1e-9:
                rows.append({"symbol": sym, "weight": round(float(v) * cfg.sleeve_weight(sl), 6),
                             "sleeve": sl.id})
    return rows


def _completed(close: pd.DataFrame, today: pd.Timestamp) -> pd.DataFrame:
    """Rows of finished sessions only, plus one placeholder row when the
    month is still running. A run during US hours can see today's half-day
    bar, which must not drive a decision; and without the placeholder the
    last row would look like a month end every day, so tranche 0 would
    re-decide daily."""
    px = close[close.index < today.normalize()]
    nxt = px.index[-1] + pd.offsets.BDay(1)
    if nxt.to_period("M") == px.index[-1].to_period("M"):
        px = pd.concat([px, px.iloc[[-1]].set_axis([nxt])])
    return px


def _tranche_params(cfg: TraderConfig) -> dict:
    stable, aggr = SLEEVES
    params = dict(strategy="canary_gate" if cfg.canary else "momentum_blend",
                  tranches=cfg.tranches, spacing=cfg.tranche_spacing,
                  lookback=cfg.lookback, lookbacks=list(cfg.lookbacks) or None,
                  stable_assets=stable.universe, aggr_assets=aggr.universe, safe=SAFE)
    if cfg.canary:
        params["canary"] = list(cfg.canary)
    return params


def model_weights(close: pd.DataFrame, cfg: TraderConfig) -> pd.DataFrame:
    """Daily target weights of the whole portfolio, as the backtest engine
    computes them for this config (cash buffer included)."""
    if cfg.staggered:
        w = tranched(close, aggr_share=cfg.aggr_share, **_tranche_params(cfg))
    else:
        stable, aggr = SLEEVES
        w = momentum_blend(close, aggr_share=cfg.aggr_share, lookback=cfg.lookback,
                           lookbacks=list(cfg.lookbacks) or None, safe=SAFE,
                           stable_assets=stable.universe, aggr_assets=aggr.universe)
    return w.reindex(close.index).ffill().fillna(0.0) * (1 - cfg.cash_buffer)


def shadow_backtest(close: pd.DataFrame, cfg: TraderConfig, rows: list[dict],
                    cost_bps: float) -> dict | None:
    """What the backtest says the account would be worth on each recorded day,
    starting from the first recorded equity. Holdings decided at one close earn
    the next session's return, minus `cost_bps` per unit of turnover."""
    if not rows:
        return None
    d0, e0 = pd.Timestamp(rows[0]["date"]), float(rows[0]["equity"])
    w = model_weights(close, cfg)
    ret = close[w.columns].pct_change().fillna(0.0)
    net = (w.shift(1).fillna(0.0) * ret).sum(axis=1) - w.diff().abs().sum(axis=1) * cost_bps / 10_000
    value = e0 * (1 + net[net.index > d0]).cumprod()
    series = {rows[0]["date"]: round(e0, 2)}
    for r in rows[1:]:
        d = pd.Timestamp(r["date"])
        if d in value.index:
            series[r["date"]] = round(float(value[d]), 2)
    return {"from": rows[0]["date"], "cost_bps": cost_bps, "series": series}


def staggered_targets(close: pd.DataFrame, today: pd.Timestamp,
                      cfg: TraderConfig) -> tuple[list[dict], pd.Timestamp]:
    """Target weights per sleeve for the canary/tranche strategy, as decided
    by the latest finished session. Returns (rows, that session's date)."""
    px = _completed(close, today)
    last = close.index[close.index < today.normalize()][-1]
    stable, aggr = SLEEVES
    params = _tranche_params(cfg)
    rows = []
    # Sleeve weights are linear in aggr_share, so run each sleeve alone
    # (0 = stable only, 1 = aggressive only) to keep the sleeve labels.
    for sl, share in ((stable, 0.0), (aggr, 1.0)):
        w = tranched(px, aggr_share=share, **params).loc[last]
        for sym, v in w.items():
            if v > 1e-9:
                rows.append({"symbol": sym, "weight": round(float(v) * cfg.sleeve_weight(sl), 6),
                             "sleeve": sl.id})
    return rows, last


def canary_status(close: pd.DataFrame, sig: pd.Timestamp, cfg: TraderConfig) -> list[dict]:
    mom = momentum_13612(close.loc[:sig, list(cfg.canary)]).iloc[-1]
    return [{"symbol": c, "score": round(float(mom[c]), 4), "risk_on": bool(mom[c] > 0)}
            for c in cfg.canary]


def signal_scores(close: pd.DataFrame, sig: pd.Timestamp, cfg: TraderConfig,
                  targets: list[dict]) -> list[dict]:
    chosen = {(t["sleeve"], t["symbol"]) for t in targets}
    ret = cfg.momentum(close.loc[:sig]).loc[sig]
    return [{"symbol": sym, "sleeve": sl.id, "return": round(float(ret[sym]), 4),
             "selected": (sl.id, sym) in chosen}
            for sl in SLEEVES for sym in sl.universe]


def next_rebalance(today: pd.Timestamp, cfg: TraderConfig | None = None) -> dict:
    """Approximate: last weekday of this month, then the next weekday
    (US holidays are not modelled). With tranches, the next of month end,
    +spacing, +2*spacing, ... weekdays after it."""
    month_end = today + pd.offsets.BMonthEnd(0)
    if cfg is not None and cfg.tranches > 1:
        prev_end = today - pd.offsets.BMonthEnd(1)
        days = [end + pd.offsets.BDay(k * cfg.tranche_spacing)
                for end in (prev_end, month_end) for k in range(cfg.tranches)]
        # the next tranche whose order day (signal day + 1) is today or later
        month_end = min(d for d in days if d + pd.offsets.BDay(1) >= today.normalize())
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
    LIVE_MODES = ("kiwoom_live",)

    def __init__(self, broker, cfg: TraderConfig | None = None, mode: str = "simulate",
                 notify=None):
        self.broker, self.cfg, self.mode = broker, cfg or TraderConfig(), mode
        # Real-money orders are announced on Telegram right before they go out
        # (the user chose an alert over a confirmation step, 2026-10-10).
        self.notify = notify
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

    def _last_holdings(self) -> tuple[dict[str, dict], float | None]:
        """Holdings from the previous state.json, shown when the broker can't
        be read so a failed lookup doesn't look like an empty account."""
        prev = self._read("state.json")
        positions = {p["symbol"]: {"qty": p["qty"], "avg_price": p["avg_price"]}
                     for p in prev.get("positions", [])}
        return positions, prev.get("cash")

    def _reconcile(self, positions: dict[str, dict]) -> None:
        """Brokers like Kiwoom only accept an order; the fill shows up later in
        the balance. Settle earlier "pending" trades from current holdings:
        compared with `held_before` (shares held when the order went out), a
        buy is filled once all its shares have arrived (price = the broker's
        average cost), partial while some have; a sell is filled once its
        shares are gone. Anything else stays pending."""
        p = self.dir / "trades.jsonl"
        if not p.exists():
            return
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        changed = False
        for r in rows:
            if r.get("status") not in ("pending", "partial"):
                continue
            held = positions.get(r["symbol"], {}).get("qty", 0)
            before = r.get("held_before", 0)
            if r["side"] == "buy" and held >= before + r["qty"]:
                r["status"], r["price"] = "filled", round(positions[r["symbol"]]["avg_price"], 4)
            elif r["side"] == "buy" and held > before:
                r["status"] = "partial"
            elif r["side"] == "sell" and held <= before - r["qty"]:
                r["status"] = "filled"
            else:
                continue
            r.pop("message", None)
            changed = True
        if changed:
            tmp = self.dir / ".trades.jsonl.tmp"
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                           encoding="utf-8")
            os.replace(tmp, p)

    def _settle_fees(self, meta: dict, cash: float) -> None:
        """Kiwoom reports neither fills nor fees, but takes the fee out of cash.
        Once every order of the last batch has settled, the fee is the cash
        that left beyond the traded value: cash_before - cash - buys + sells.
        It is split over the batch by traded value. Batches whose broker already
        reported fees (the simulator) are left alone."""
        batch = meta.get("fee_batch")
        p = self.dir / "trades.jsonl"
        if not batch or not p.exists():
            return
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        mine = [r for r in rows if r.get("time") == batch["time"] and r.get("status") != "rejected"]
        if any(r.get("status") in ("pending", "partial") for r in mine):
            return  # wait for the fills
        meta.pop("fee_batch", None)
        if not mine or any(r.get("fee") for r in mine):
            return
        value = [r["qty"] * r["price"] for r in mine]
        net = sum(v if r["side"] == "buy" else -v for r, v in zip(mine, value))
        fee, total = batch["cash_before"] - cash - net, sum(value)
        if not (0 < fee <= 0.05 * total):  # anything else is not a fee (deposit, FX, missed fill)
            return
        for r, v in zip(mine, value):
            r["fee"] = round(fee * v / total, 2)
        tmp = self.dir / ".trades.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        os.replace(tmp, p)

    def _switch_strategy(self, meta: dict, today: pd.Timestamp) -> dict:
        """When the configured strategy differs from the one that wrote the
        records, move the old trades/equity/meta into archive/<old id>_<date>/
        and start a fresh record (start date and capital = today). Holdings
        stay; this run's rebalance turns them into the new targets."""
        # Records from before strategy_id was stored (paper trading began
        # 2026-10-09) were all written by the monthly multi-window blend.
        old = meta.get("strategy_id", "momentum_blend_multi")
        if not meta or old in (None, self.cfg.strategy_id):
            return meta
        dest = self.dir / "archive" / f"{old}_{today.date()}"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("trades.jsonl", "equity.jsonl", "meta.json"):
            if (self.dir / name).exists():
                os.replace(self.dir / name, dest / name)
        return {}

    def _equity_row(self, rec: dict) -> None:
        p = self.dir / "equity.jsonl"
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
        rows = [r for r in rows if r["date"] != rec["date"]] + [rec]
        rows.sort(key=lambda r: r["date"])
        tmp = self.dir / ".equity.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        os.replace(tmp, p)

    def _prev_equity(self, before: str) -> float | None:
        """Equity recorded at the last close before `before` (YYYY-MM-DD)."""
        p = self.dir / "equity.jsonl"
        if not p.exists():
            return None
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        rows = [r for r in rows if r.get("date", "") < before and r.get("equity")]
        return float(rows[-1]["equity"]) if rows else None

    def _guard(self, orders: list[dict], equity: float, last_date: str) -> str | None:
        """Why this batch must not go out, or None. Checked before every order run."""
        live = self.mode in self.LIVE_MODES
        if live and not self.cfg.max_order_usd:
            return "실계좌는 주문 한도(MAX_ORDER_USD)를 .env 에 정해야 주문합니다."
        limit = self.cfg.max_order_usd or equity
        too_big = [o for o in orders if o["value"] > limit]
        if too_big:
            return (f"주문 한도 초과로 차단: {too_big[0]['symbol']} ${too_big[0]['value']:,.0f}"
                    f" (한도 ${limit:,.0f})")
        prev = self._prev_equity(last_date)
        if self.cfg.max_daily_loss and prev and equity / prev - 1 <= -self.cfg.max_daily_loss:
            return (f"하루 손실 한도 초과로 오늘은 주문하지 않았습니다: 전날 ${prev:,.0f} → 지금 "
                    f"${equity:,.0f} ({(equity / prev - 1) * 100:+.1f}%, 한도 "
                    f"-{self.cfg.max_daily_loss * 100:.0f}%). 내일 다시 확인합니다.")
        return None

    def _announce(self, orders: list[dict]) -> None:
        if self.mode not in self.LIVE_MODES or not self.notify:
            return
        lines = [f"{'🔴 매수' if o['side'] == 'buy' else '🔵 매도'} {o['symbol']} {o['qty']}주 "
                 f"(약 ${o['value']:,.0f})" for o in orders]
        try:
            self.notify("💰 Mintory 실계좌 주문을 보냅니다\n\n" + "\n".join(lines))
        except Exception:  # noqa: BLE001 - an alert must never stop or crash trading
            pass

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
            tickers = sorted(set(DATA_TICKERS) | set(self.cfg.canary))
            close = load_prices(tickers, "2009-01-01", use_cache=False).dropna()
        last = close.index[-1]
        prices = {t: float(close[t].iloc[-1]) for t in close.columns}
        age = (today - last.normalize()).days
        if age > self.cfg.max_data_age_days:
            alerts.append({"level": "error",
                           "message": f"가격 데이터가 {age}일 전 것이라 매매하지 않았습니다."})
            return self._state(now, meta, alerts=alerts, prices=prices)

        if self.cfg.staggered:
            targets, sig = staggered_targets(close, today, self.cfg)
        else:
            sig = signal_date(close.index, today)
            targets = sleeve_targets(close, sig, self.cfg) if sig is not None else []
        scores = signal_scores(close, sig, self.cfg, targets) if sig is not None else []
        canary = canary_status(close, sig, self.cfg) if self.cfg.canary and sig is not None else None
        weights: dict[str, float] = {}
        for t in targets:
            weights[t["symbol"]] = weights.get(t["symbol"], 0.0) + t["weight"]

        try:
            positions, cash = self.broker.positions(), self.broker.cash_usd()
        except Exception as e:  # noqa: BLE001 - report instead of crashing
            alerts.append({"level": "error", "message":
                           f"계좌 조회에 실패해 매매하지 않았습니다: {str(e)[:200]}"})
            positions, cash = self._last_holdings()
            return self._state(now, meta, alerts=alerts, positions=positions, cash=cash,
                               prices=prices, targets=targets, scores=scores, sig=sig, canary=canary)
        self._reconcile(positions)
        self._settle_fees(meta, cash)
        if execute:  # a --check run must not move or re-label the records
            meta = self._switch_strategy(meta, today)
            meta.setdefault("strategy_id", self.cfg.strategy_id)
        meta.setdefault("started_at", str(today.date()))
        meta.setdefault("initial_capital", cash + sum(p["qty"] * prices.get(s, 0.0)
                                                      for s, p in positions.items()))
        if self.cfg.staggered:
            # Tranches change on their own days; trade whenever the target
            # differs from the one last executed.
            wanted = {k: round(v, 4) for k, v in sorted(weights.items())}
            due = meta.get("last_weights") != wanted
        else:
            due = sig is not None and meta.get("last_signal") != str(sig.date())

        if due:
            orders = plan_orders(weights, prices, positions, cash, self.cfg)
            equity = cash + sum(p["qty"] * prices.get(s, 0.0) for s, p in positions.items())
            blocked = self._guard(orders, equity, str(last.date())) if orders else None
            if blocked:
                alerts.append({"level": "error", "message": blocked})
            elif not execute:
                for o in orders:
                    alerts.append({"level": "info", "message":
                                   f"점검 모드 - {o['side']} {o['symbol']} {o['qty']}주 예정"})
            else:
                sleeve_of = {t["symbol"]: t["sleeve"] for t in targets}
                meta["fee_batch"] = {"time": now.isoformat(timespec="seconds"), "cash_before": cash}
                self._announce(orders)
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
                           "sleeve": sleeve_of.get(o["symbol"]),
                           "note": "분할 리밸런싱" if self.cfg.staggered else "월간 리밸런싱",
                           "held_before": positions.get(o["symbol"], {}).get("qty", 0)}
                    if not ok:
                        rec["message"] = res.get("msg", "")
                        alerts.append({"level": "warn", "message":
                                       f"{o['symbol']} {'매수' if o['side'] == 'buy' else '매도'} "
                                       + ("주문 접수를 확인하지 못했습니다"
                                          if rec["status"] == "pending" else "주문이 거부되었습니다")
                                       + f" ({res.get('msg', '')})"})
                    self._append("trades.jsonl", rec)
                if all_ok:
                    meta["last_signal"] = str(sig.date())
                    meta["last_rebalance"] = str(today.date())
                    if self.cfg.staggered:
                        meta["last_weights"] = wanted
                positions, cash = self.broker.positions(), self.broker.cash_usd()
                self._reconcile(positions)
                self._settle_fees(meta, cash)

        equity = cash + sum(p["qty"] * prices.get(s, 0.0) for s, p in positions.items())
        self._equity_row({"date": str(last.date()), "equity": round(equity, 2),
                          "cash": round(cash, 2),
                          "benchmark_close": round(prices.get(BENCHMARK, float("nan")), 4)})
        self._write_atomic("meta.json", meta)
        backtest = None
        try:
            cost = self.cfg.model_cost_bps
            if cost is None:
                cost = 25.0 if self.mode == "kiwoom_mock" else 10.0
            backtest = shadow_backtest(close, self.cfg, self._equity_rows(), cost)
        except Exception as e:  # noqa: BLE001 - the comparison must never stop a run
            alerts.append({"level": "info", "message": f"백테스트 비교를 계산하지 못했습니다: {str(e)[:120]}"})
        return self._state(now, meta, alerts=alerts, positions=positions, cash=cash,
                           prices=prices, targets=targets, scores=scores, sig=sig, canary=canary,
                           backtest=backtest)

    def _extra_rules(self) -> list[str]:
        rules = []
        if self.cfg.tranches > 1:
            rules.append(f"돈을 {self.cfg.tranches}덩어리로 나눠 월말, " + ", ".join(
                f"+{k * self.cfg.tranche_spacing}" for k in range(1, self.cfg.tranches))
                + "거래일에 한 덩어리씩 리밸런싱합니다.")
        if self.cfg.canary:
            rules.append(f"리밸런싱 날 {'·'.join(self.cfg.canary)}의 1·3·6·12개월 가중 모멘텀이 "
                         f"0 이하면 그 덩어리는 전부 {SAFE}로 피합니다.")
        return rules

    def _equity_rows(self) -> list[dict]:
        p = self.dir / "equity.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _state(self, now, meta, alerts, positions=None, cash=None, prices=None,
               targets=None, scores=None, sig=None, canary=None, backtest=None) -> dict:
        prices = prices or {}
        if positions is None:
            try:
                positions, cash = self.broker.positions(), self.broker.cash_usd()
            except Exception:  # noqa: BLE001 - state must still be written
                positions, cash = self._last_holdings()
        sleeve_of = {t["symbol"]: t["sleeve"] for t in (targets or [])}
        state = {
            "schema_version": 1,
            "strategy": {
                "id": self.cfg.strategy_id,
                "name": self.cfg.strategy_name,
                "description": " ".join([
                    "1·3·6·12개월 수익률의 평균으로 순위를 매겨 안정 바구니는 1등, "
                    "공격 바구니는 1·2등을 반씩 담습니다. 평균이 0 이하면 IEF."
                    if self.cfg.lookbacks else
                    "안정 바구니는 3개월 수익률 1등, 공격 바구니는 1·2등을 반씩. "
                    "수익률이 마이너스면 IEF.", *self._extra_rules()]),
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
            "next_rebalance": next_rebalance(pd.Timestamp(now.date()), self.cfg),
            "signal": {"as_of": str(sig.date()) if sig is not None else None,
                       "lookback_days": max(self.cfg.lookbacks or (self.cfg.lookback,)),
                       "lookbacks": list(self.cfg.lookbacks or (self.cfg.lookback,)),
                       "scores": scores or []},
            "alerts": alerts,
        }
        if self.cfg.canary and canary is not None:
            state["signal"]["canary"] = canary
        if backtest is None:  # early exits (pause, stale data) keep the last comparison
            backtest = self._read("state.json").get("backtest")
        if backtest:
            state["backtest"] = backtest
        self._write_atomic("state.json", state)
        return state
