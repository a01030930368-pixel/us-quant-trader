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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quant import market_calendar as mc
from quant.data import load_prices
from quant.strategies import REGISTRY, _period_ends, momentum_13612, momentum_blend, momentum_rotation, tranched

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
SAFE = "IEF"


def fetch_usdkrw() -> float | None:
    """The latest USD/KRW close from Yahoo ("KRW=X"), or None. Only for showing
    balances and estimating tax in won; never used for trading decisions."""
    try:
        import yfinance as yf
        raw = yf.download("KRW=X", period="10d", interval="1d", progress=False, auto_adjust=False)
        col = raw["Close"].dropna()
        val = float(col.iloc[-1, 0] if getattr(col, "ndim", 1) == 2 else col.iloc[-1])
        return round(val, 2) if 500 < val < 5000 else None
    except Exception:  # noqa: BLE001 - won display is optional
        return None
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
    # Invest idle cash (a deposit, sale leftovers) once it exceeds this share of
    # the account, buying toward the current targets without selling anything.
    idle_cash_pct: float = 0.02
    max_data_age_days: int = 5         # refuse to trade on stale prices
    # An order Kiwoom never answered is checked against the balance after
    # retry_wait_s seconds and sent again if it isn't there, up to this many
    # times, only while the US regular session is open (2026-10-12: a TQQQ buy
    # got no reply and would otherwise have waited a whole day).
    order_retries: int = 2
    retry_wait_s: float = 60.0
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


def _fill_yesterday(close: pd.DataFrame, tickers: list[str], today: pd.Timestamp) -> pd.DataFrame:
    """Yahoo sometimes leaves out the previous session while today's live bar is
    there (seen on the PC 2026-10-22). A download that ends before today only
    asks for finished sessions; take the missing rows from it."""
    want = today.normalize() - mc.bday()
    done = close.index[close.index < today.normalize()]
    if len(done) and done[-1].normalize() >= want:
        return close
    try:
        hist = load_prices(tickers, "2009-01-01", end=str(today.date()), use_cache=False).dropna()
    except Exception:  # noqa: BLE001 - the check in run() reports it
        return close
    extra = hist.loc[~hist.index.isin(close.index)]
    return pd.concat([close, extra]).sort_index() if len(extra) else close


def _yf_version() -> str:
    """For the late-data alert: which yfinance the PC runs, to tell a Yahoo delay from a library bug."""
    try:
        import yfinance
        return f", yfinance {yfinance.__version__}"
    except Exception:  # noqa: BLE001 - only a hint in a message
        return ""


def session_cutoff(now: datetime) -> pd.Timestamp:
    """The date before which every daily bar is a finished US session.

    Usually the KST date. Between KST midnight and the US close (a catch-up
    run, or a manual run after midnight) the KST date is already tomorrow while
    the US session is still trading, so its half-day bar would look finished;
    the US Eastern date is used then instead."""
    et = now.astimezone(ET)
    day = et.date() + timedelta(days=1) if (et.hour, et.minute) >= (16, 30) else et.date()
    return pd.Timestamp(day)


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
    nxt = px.index[-1] + mc.bday()
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
    growth = (1 + net[net.index > d0]).cumprod()
    # Deposits and withdrawals (row "flow") go into the model account on the same day.
    series = {rows[0]["date"]: round(e0, 2)}
    v, g_prev = e0, 1.0
    for r in rows[1:]:
        d = pd.Timestamp(r["date"])
        if d in growth.index:
            g = float(growth[d])
            v = v * g / g_prev + float(r.get("flow") or 0)
            g_prev = g
            series[r["date"]] = round(v, 2)
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


def tranche_detail(close: pd.DataFrame, today: pd.Timestamp, cfg: TraderConfig) -> list[dict]:
    """What each tranche holds now, for the dashboard: the day it last rebalanced,
    whether the canary allowed risk on that day, and its weights (of that tranche)."""
    px = _completed(close, today)
    last = close.index[close.index < today.normalize()][-1]
    params = _tranche_params(cfg)
    strategy, n, spacing = params.pop("strategy"), params.pop("tranches"), params.pop("spacing")
    out = []
    for k in range(n):
        w = REGISTRY[strategy](px, offset=k * spacing, aggr_share=cfg.aggr_share, **params).loc[last]
        ends = _period_ends(px.index, "M", k * spacing)
        ends = ends[ends <= last]
        decided = ends[-1] if len(ends) else None
        risk_on = None
        if cfg.canary and decided is not None:
            mom = momentum_13612(px.loc[:decided, list(cfg.canary)]).iloc[-1]
            risk_on = bool((mom > 0).all())
        out.append({"n": k + 1, "decided": str(decided.date()) if decided is not None else None,
                    "risk_on": risk_on,
                    "weights": {t: round(float(v), 4) for t, v in w.items() if v > 1e-9}})
    return out


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
    """Last US trading day of this month, then the next trading day (NYSE
    holidays included, see quant.market_calendar). With tranches, the next of
    month end, +spacing, +2*spacing, ... trading days after it."""
    month_end = today + mc.bmonth_end() * 0
    if cfg is not None and cfg.tranches > 1:
        prev_end = today - mc.bmonth_end()
        days = [(end + mc.bday() * (k * cfg.tranche_spacing), k)
                for end in (prev_end, month_end) for k in range(cfg.tranches)]
        # the next tranche whose order day (signal day + 1) is today or later
        month_end, k = min(d for d in days if d[0] + mc.bday() >= today.normalize())
        return {"signal_date": str(month_end.date()),
                "order_date": str((month_end + mc.bday()).date()),
                "tranche": k + 1, "tranches": cfg.tranches}
    return {"signal_date": str(month_end.date()),
            "order_date": str((month_end + mc.bday()).date())}


def preview_rebalance(close: pd.DataFrame, today: pd.Timestamp, cfg: TraderConfig,
                      positions: dict[str, dict], cash: float, prices: dict[str, float],
                      ran_today: bool = True) -> dict:
    """The next rebalance, if prices stayed where they are now: the target
    weights and the orders it would place. Unfinished sessions are dropped and
    the last close is carried forward to the signal day.

    ran_today: today's order run already happened, so show the one after it.
    A check run earlier in the day (a refresh at noon KST) shows tonight's."""
    nxt = next_rebalance(today, cfg)
    if ran_today and nxt["order_date"] <= str(today.date()):
        nxt = next_rebalance(today + mc.bday(), cfg)
    sig = pd.Timestamp(nxt["signal_date"])
    px = close[close.index < today.normalize()]
    if sig > px.index[-1]:
        future = pd.date_range(px.index[-1] + mc.bday(), sig, freq=mc.bday())
        px = pd.concat([px, pd.DataFrame([px.iloc[-1].values] * len(future), index=future, columns=px.columns)])
    # Same rows the order day will see. Without the placeholder the carried-
    # forward signal day would look like a month end, and the month-end
    # tranche would re-decide early (2026-10-15: half the account shown
    # moving to IEF on 10/22 instead of one quarter).
    px = _completed(px.loc[:sig], sig + mc.bday())
    w = model_weights(px, cfg).loc[sig] / (1 - cfg.cash_buffer)
    weights = {t: round(float(v), 4) for t, v in w.items() if v > 1e-9}
    orders = plan_orders(weights, {**{t: float(px[t].iloc[-1]) for t in px.columns}, **prices},
                         positions, cash, cfg)
    return {**nxt, "as_of": str(close.index[close.index < today.normalize()][-1].date()),
            "weights": weights,
            "orders": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items()} for o in orders]}


def plan_orders(weights: dict[str, float], prices: dict[str, float],
                positions: dict[str, dict], cash: float, cfg: TraderConfig,
                reserve: float = 0.0) -> list[dict]:
    """Orders that bring holdings to `weights`. `reserve` dollars (money set
    aside for a withdrawal) are kept in cash and not invested."""
    equity = cash + sum(p["qty"] * prices[t] for t, p in positions.items())
    investable = max(0.0, equity - reserve) * (1 - cfg.cash_buffer)
    orders = []
    for t in sorted(set(weights) | set(positions)):
        target = math.floor(investable * weights.get(t, 0.0) / prices[t])
        diff = target - positions.get(t, {}).get("qty", 0)
        if diff == 0 or abs(diff) * prices[t] < cfg.min_trade_usd:
            continue
        orders.append({"symbol": t, "side": "buy" if diff > 0 else "sell",
                       "qty": abs(diff), "ref_price": prices[t], "value": abs(diff) * prices[t]})
    return sorted(orders, key=lambda o: o["side"] != "sell")  # sells fund buys


def buy_only(orders: list[dict], budget: float, min_trade: float = 0.0) -> list[dict]:
    """The buys of `orders`, shrunk so they fit in `budget` dollars (biggest first)."""
    out = []
    for o in sorted((o for o in orders if o["side"] == "buy"), key=lambda o: -o["value"]):
        qty = min(o["qty"], math.floor(budget / o["ref_price"]))
        if qty <= 0 or qty * o["ref_price"] < min_trade:
            continue
        out.append({**o, "qty": qty, "value": qty * o["ref_price"]})
        budget -= qty * o["ref_price"]
    return out


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
        # USD/KRW for the won view and the tax estimate. Fetched only for real runs
        # (tests pass prices in and may set this to a lambda).
        self.fx_source = None
        self.fx: float | None = None
        self.sleep = time.sleep  # tests replace it

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
        if (prev.get("cash") or 0) < 0:  # never fall back to a broken balance
            prev["cash"] = (self._read("meta.json").get("seen") or {}).get("cash")
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

    def _supersede(self, orders: list[dict]) -> None:
        """Orders still "pending" (no shares arrived yet) for a symbol and side
        that is ordered again now are replaced: the new plan was made from the
        actual balance, so the old order never went through. Without this the
        new fill would settle the old record too (2026-10-12: a TQQQ buy that got
        no reply was counted twice)."""
        p = self.dir / "trades.jsonl"
        again = {(o["symbol"], o["side"]) for o in orders}
        if not p.exists() or not again:
            return
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        changed = False
        for r in rows:
            if r.get("status") == "pending" and (r.get("symbol"), r.get("side")) in again:
                r["status"], r["message"] = "cancelled", "잔고에 들어오지 않아 다음 실행에서 다시 주문"
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
        mine = [r for r in rows if r.get("time") == batch["time"]
                and r.get("status") not in ("rejected", "cancelled")]
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

    # -- deposits, withdrawals and money set aside for a withdrawal ------------
    WITHDRAW = "withdraw.json"

    def _pending_orders(self) -> bool:
        p = self.dir / "trades.jsonl"
        if not p.exists():
            return False
        return any(json.loads(x).get("status") in ("pending", "partial")
                   for x in p.read_text(encoding="utf-8").splitlines() if x.strip())

    def _detect_flow(self, meta: dict, positions: dict, cash: float, prices: dict) -> float:
        """Dollars deposited (+) or withdrawn (-) since the last run, or 0.

        The last run left the cash and share counts it saw in meta["seen"].
        Cash should have moved only by what the shares bought or sold since then
        (valued at the latest prices, fees ignored); a bigger difference is money
        moved in or out (a deposit, a KRW->USD exchange, a withdrawal). Skipped
        while an order is still unfilled, because the broker may already hold
        its cash back."""
        seen = meta.get("seen")
        if not seen or self._pending_orders():
            return 0.0
        before = seen.get("qty") or {}
        traded = 0.0
        expected = float(seen.get("cash", 0.0))
        for sym in set(before) | set(positions):
            d = positions.get(sym, {}).get("qty", 0) - before.get(sym, 0)
            if d:
                px = prices.get(sym) or positions.get(sym, {}).get("avg_price") or 0.0
                expected -= d * px
                traded += abs(d) * px
        equity = cash + sum(p["qty"] * prices.get(s, 0.0) for s, p in positions.items())
        delta = cash - expected
        # Dividends, fees and fill-vs-close differences stay under this.
        if abs(delta) < max(100.0, 0.005 * equity, 0.03 * traded):
            return 0.0
        return round(delta, 2)

    def withdraw_request(self) -> dict | None:
        try:
            req = self._read(self.WITHDRAW)
        except (OSError, ValueError):
            return None
        return req if float(req.get("left_usd") or 0) > 0 else None

    def _record_flow(self, amount: float, now: datetime, row_date: str, cash: float, alerts: list) -> None:
        kind = "deposit" if amount > 0 else "withdraw"
        rec = {"time": now.isoformat(timespec="seconds"), "date": row_date, "amount": amount, "kind": kind,
               "cash_after": round(cash, 2), **({"fx": self.fx} if self.fx else {})}
        self._append("flows.jsonl", rec)
        won = f" (약 ₩{round(abs(amount) * self.fx):,})" if self.fx else ""
        alerts.append({"level": "info", "message":
                       f"{'입금' if amount > 0 else '출금'} 확인: ${abs(amount):,.2f}{won}"})
        req = self.withdraw_request()
        if amount > 0:
            text = (f"💵 입금 확인 · Mintory\n\n➕ ${amount:,.2f}{won}\n\n"
                    "💡 다음 실행 때 목표 비중대로 투자해요. 입금한 돈은 수익률에 들어가지 않아요.")
        else:
            text = f"🏧 출금 확인 · Mintory\n\n➖ ${-amount:,.2f}{won}"
            if req:
                left = round(float(req["left_usd"]) + amount, 2)
                if left <= max(20.0, 0.02 * float(req.get("amount_usd") or 0)):
                    (self.dir / self.WITHDRAW).unlink(missing_ok=True)
                    text += "\n\n✅ 요청하신 출금이 끝났어요. 남은 돈으로 같은 전략을 계속 이어가요."
                else:
                    self._write_atomic(self.WITHDRAW, {**req, "left_usd": left})
                    text += f"\n\n⏳ 빼 둔 돈 중 ${left:,.2f}가 아직 계좌에 있어요."
            text += "\n\n💡 출금한 돈은 손실로 치지 않아요. 수익률은 그대로 이어져요."
        self._tell(text)

    def _tell(self, text: str) -> None:
        if not self.notify:
            return
        try:
            self.notify(text)
        except Exception:  # noqa: BLE001 - an alert must never stop or crash trading
            pass

    def _withdraw_ready(self, cash: float, paused: bool) -> dict | None:
        """Status of the withdrawal request for state.json; tells the phone once
        when the money is set aside in cash."""
        req = self.withdraw_request()
        if not req:
            return None
        left = float(req["left_usd"])
        ready = cash >= left - 1.0
        if ready and not req.get("ready_notified"):
            won = f" (약 ₩{round(left * self.fx):,})" if self.fx else ""
            self._tell(f"💵 출금 준비 완료 · Mintory\n\n${left:,.2f}{won}를 현금으로 빼 뒀어요.\n\n"
                       "👉 키움 앱(영웅문)에서 달러를 원화로 환전한 뒤 출금하세요. "
                       "출금이 확인되면 다시 알려 드려요.")
            req = {**req, "ready_notified": True}
            self._write_atomic(self.WITHDRAW, req)
        return {**req, "ready": ready, "paused": paused and not ready}

    def _money_in(self, meta: dict) -> dict:
        """principal (starting capital + deposits - withdrawals) and the totals."""
        rows = self._equity_rows()
        dep = sum(float(r["flow"]) for r in rows if float(r.get("flow") or 0) > 0)
        wd = -sum(float(r["flow"]) for r in rows if float(r.get("flow") or 0) < 0)
        start = float(meta.get("initial_capital", self.cfg.initial_capital))
        return {"principal": round(start + dep - wd, 2),
                "flows": {"deposits": round(dep, 2), "withdrawals": round(wd, 2)}}

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
        for name in ("trades.jsonl", "equity.jsonl", "meta.json", "flows.jsonl"):
            if (self.dir / name).exists():
                os.replace(self.dir / name, dest / name)
        return {}

    def _equity_row(self, rec: dict, flow: float = 0.0) -> None:
        """Write the day's row (replacing an earlier one for the same date, whose
        deposits/withdrawals in `flow` are kept and added to)."""
        p = self.dir / "equity.jsonl"
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []
        old = next((r for r in rows if r["date"] == rec["date"]), {})
        total = float(old.get("flow") or 0) + flow
        if abs(total) >= 0.005:
            rec["flow"] = round(total, 2)
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

    def _guard(self, orders: list[dict], equity: float, last_date: str, flow: float = 0.0) -> str | None:
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
        equity -= flow  # a deposit is not a gain, a withdrawal not a loss
        if self.cfg.max_daily_loss and prev and equity / prev - 1 <= -self.cfg.max_daily_loss:
            return (f"하루 손실 한도 초과로 오늘은 주문하지 않았습니다: 전날 ${prev:,.0f} → 지금 "
                    f"${equity:,.0f} ({(equity / prev - 1) * 100:+.1f}%, 한도 "
                    f"-{self.cfg.max_daily_loss * 100:.0f}%). 내일 다시 확인합니다.")
        return None

    @staticmethod
    def _no_reply(res: dict) -> bool:
        return not res.get("ok") and res.get("status") == "pending"

    @staticmethod
    def _market_open(now: datetime, margin_min: int = 5) -> bool:
        """The US regular session is open at `now` with `margin_min` minutes to spare."""
        try:
            from zoneinfo import ZoneInfo
            et = now.astimezone(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001 - no tz data: don't guess, don't retry
            return False
        d = et.date()
        if not mc.is_trading_day(d):
            return False
        close_h = 13 if d in mc.early_closes(d.year) else 16
        minutes = et.hour * 60 + et.minute
        return 9 * 60 + 30 <= minutes <= close_h * 60 - margin_min

    def _retry_unanswered(self, unanswered: list, positions: dict, now: datetime) -> list:
        """Orders Kiwoom never answered: wait, look at the balance and send the
        ones that left no trace again. Returns (order, result) for each, the
        result being "pending" once the shares show up (or the order was taken)."""
        out = []
        for attempt in range(1, self.cfg.order_retries + 1):
            if not unanswered or not self._market_open(now):
                break
            self.sleep(self.cfg.retry_wait_s)
            try:
                held = self.broker.positions()
            except Exception:  # noqa: BLE001 - can't tell, leave them pending
                break
            still = []
            for o, res in unanswered:
                before = positions.get(o["symbol"], {}).get("qty", 0)
                qty = held.get(o["symbol"], {}).get("qty", 0)
                if (qty > before) if o["side"] == "buy" else (qty < before):
                    out.append((o, {"ok": True, "status": "pending", "fill_price": o["ref_price"],
                                    "retried": attempt - 1}))
                    continue
                res = self.broker.place_order(o["symbol"], o["side"], o["qty"], o["ref_price"])
                res["retried"] = attempt
                (still if self._no_reply(res) else out).append((o, res))
            unanswered = still
        return out + unanswered

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
        today = session_cutoff(now)
        self.planned = None  # a --check run records the orders it would send
        self.closes = None
        meta = self._read("meta.json")
        alerts: list[dict] = []

        # Paused: no orders, but the balance and the day's equity row are still
        # recorded so the history has no gaps (a --check run with a red flag).
        paused = (self.dir / "STOP").exists()
        if paused:
            alerts.append({"level": "error", "message": "STOP 파일이 있어 매매를 멈췄습니다."})
            execute = False

        fx_source = self.fx_source
        if close is None:
            tickers = sorted(set(DATA_TICKERS) | set(self.cfg.canary))
            close = load_prices(tickers, "2009-01-01", use_cache=False).dropna()
            close = _fill_yesterday(close, tickers, today)
            fx_source = fx_source or fetch_usdkrw
        self.fx = fx_source() if fx_source else None
        last = close.index[-1]
        prices = {t: float(close[t].iloc[-1]) for t in close.columns}
        # The last bar's date and the bar before it, for the daily close report.
        prev = close.iloc[-2] if len(close) > 1 else None
        self.closes = {"date": str(last.date()),
                       "prev_date": str(close.index[-2].date()) if prev is not None else None,
                       "prev": {t: round(float(prev[t]), 4) for t in close.columns} if prev is not None else {}}
        age = (today - last.normalize()).days
        if age > self.cfg.max_data_age_days:
            alerts.append({"level": "error",
                           "message": f"가격 데이터가 {age}일 전 것이라 매매하지 않았습니다."})
            return self._state(now, meta, alerts=alerts, prices=prices)

        # Yahoo can lag a session behind (2026-10-22: the 23:45 run saw closes
        # only up to 10/20, so tranche 4's 10/21 decision never happened and
        # the order day passed without orders). Don't trade on such data; the
        # agent retries a few minutes later and the next night catches up.
        # During US hours the last row is today's live bar, so look at the last
        # finished session (2026-10-23 00:15 KST: the PC had 10/20 and a live
        # 10/22 row, but no 10/21).
        want = today.normalize() - mc.bday()
        done = close.index[close.index < today.normalize()]
        got = done[-1] if len(done) else None
        if execute and (got is None or got.normalize() < want):
            seen = f"{got.month}/{got.day}" if got is not None else "없음"
            alerts.append({"level": "error", "message":
                           f"가격 데이터가 {want.month}/{want.day} 종가까지 들어오지 않아 "
                           f"(마지막 {seen}{_yf_version()}) 매매하지 않았습니다. "
                           "잠시 뒤 다시 시도해요."})
            return self._state(now, meta, alerts=alerts, prices=prices)

        if self.cfg.staggered:
            targets, sig = staggered_targets(close, today, self.cfg)
        else:
            sig = signal_date(close.index, today)
            targets = sleeve_targets(close, sig, self.cfg) if sig is not None else []
        scores = signal_scores(close, sig, self.cfg, targets) if sig is not None else []
        canary = canary_status(close, sig, self.cfg) if self.cfg.canary and sig is not None else None
        parts = None
        if self.cfg.staggered:
            try:
                parts = tranche_detail(close, today, self.cfg)
            except Exception:  # noqa: BLE001 - display only, never stops a run
                parts = None
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
                               prices=prices, targets=targets, scores=scores, sig=sig, canary=canary, parts=parts)
        if cash < 0:
            # A cash account can't go below zero. Kiwoom's mock server sent
            # -$95,662 one morning (2026-10-13), which read as a huge withdrawal.
            # Treat it like a failed lookup: no orders, no records changed.
            detail = getattr(self.broker, "cash_detail", None)
            alerts.append({"level": "error", "message":
                           f"키움이 달러 예수금을 ${cash:,.2f}로 보내왔어요. 나올 수 없는 값이라 "
                           "지난 기록으로 보여 주고, 이번에는 주문·기록을 하지 않았어요."
                           + (f" (키움 응답: {detail})" if detail else "")})
            positions, cash = self._last_holdings()
            return self._state(now, meta, alerts=alerts, positions=positions, cash=cash,
                               prices=prices, targets=targets, scores=scores, sig=sig, canary=canary, parts=parts)
        self._reconcile(positions)
        self._settle_fees(meta, cash)
        flow = self._detect_flow(meta, positions, cash, prices)
        if flow:
            self._record_flow(flow, now, str(last.date()), cash, alerts)
        if execute:  # a --check run must not move or re-label the records
            meta = self._switch_strategy(meta, today)
            meta.setdefault("strategy_id", self.cfg.strategy_id)
        meta.setdefault("started_at", str(today.date()))
        meta.setdefault("initial_capital", cash + sum(p["qty"] * prices.get(s, 0.0)
                                                      for s, p in positions.items()))
        if prices.get(BENCHMARK):  # SPY at the moment the money went in, for a fair comparison
            meta.setdefault("benchmark_start", round(prices[BENCHMARK], 4))
        if self.cfg.staggered:
            # Tranches change on their own days; trade whenever the target
            # differs from the one last executed.
            wanted = {k: round(v, 4) for k, v in sorted(weights.items())}
            rebalance = meta.get("last_weights") != wanted
        else:
            rebalance = sig is not None and meta.get("last_signal") != str(sig.date())

        # Besides the strategy's own rebalance days: free up cash for a requested
        # withdrawal, or put idle cash (e.g. a deposit) to work by buying only.
        req = self.withdraw_request()
        reserve = float(req["left_usd"]) if req else 0.0
        equity = cash + sum(p["qty"] * prices.get(s, 0.0) for s, p in positions.items())
        waiting = self._pending_orders()  # unfilled orders may already hold cash back
        raise_cash = reserve > 0 and cash < reserve - 1.0 and not waiting
        idle = cash - reserve - equity * self.cfg.cash_buffer
        invest_idle = bool(weights) and not waiting and idle > max(self.cfg.idle_cash_pct * equity, self.cfg.min_trade_usd)
        due = rebalance or raise_cash or invest_idle
        note = ("분할 리밸런싱" if self.cfg.staggered else "월간 리밸런싱") if rebalance else \
            "출금 준비" if raise_cash else "남는 현금 투자"

        if due:
            orders = plan_orders(weights, prices, positions, cash, self.cfg, reserve=reserve)
            if not (rebalance or raise_cash):
                orders = buy_only(orders, idle, self.cfg.min_trade_usd)
            today_flow = flow + sum(float(r.get("flow") or 0) for r in self._equity_rows()
                                    if r.get("date") == str(last.date()))
            blocked = self._guard(orders, equity, str(last.date()), today_flow) if orders else None
            if blocked:
                alerts.append({"level": "error", "message": blocked})
            elif paused:
                pass  # the STOP alert above already says why nothing was ordered
            elif not execute:
                self.planned = orders
                for o in orders:
                    alerts.append({"level": "info", "message":
                                   f"점검 모드 - {o['side']} {o['symbol']} {o['qty']}주 예정"})
            else:
                sleeve_of = {t["symbol"]: t["sleeve"] for t in targets}
                meta["fee_batch"] = {"time": now.isoformat(timespec="seconds"), "cash_before": cash}
                self._announce(orders)
                self._supersede(orders)
                all_ok = True

                def record(o: dict, res: dict) -> bool:
                    ok = res.get("ok", False)
                    rec = {"time": now.isoformat(timespec="seconds"), "date": str(last.date()),
                           "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                           "price": round(res.get("fill_price", o["ref_price"]), 4),
                           "fee": round(res.get("fee", 0.0), 2),
                           "status": res.get("status", "filled" if ok else "rejected"),
                           "order_id": res.get("order_no"),
                           "sleeve": sleeve_of.get(o["symbol"]),
                           "note": note,
                           **({"fx": self.fx} if self.fx else {}),
                           "held_before": positions.get(o["symbol"], {}).get("qty", 0)}
                    if res.get("retried"):
                        rec["retried"] = res["retried"]
                    if not ok:
                        rec["message"] = res.get("msg", "")
                        alerts.append({"level": "warn", "message":
                                       f"{o['symbol']} {'매수' if o['side'] == 'buy' else '매도'} "
                                       + ("주문 접수를 확인하지 못했습니다"
                                          if rec["status"] == "pending" else "주문이 거부되었습니다")
                                       + f" ({res.get('msg', '')})"})
                    self._append("trades.jsonl", rec)
                    return ok

                unanswered = []  # (order, last result): Kiwoom never replied
                for o in orders:
                    res = self.broker.place_order(o["symbol"], o["side"], o["qty"], o["ref_price"])
                    if self._no_reply(res):
                        unanswered.append((o, res))
                    else:
                        all_ok &= record(o, res)
                for o, res in self._retry_unanswered(unanswered, positions, now):
                    all_ok &= record(o, res)
                if all_ok and rebalance:
                    meta["last_signal"] = str(sig.date())
                    meta["last_rebalance"] = str(today.date())
                    if self.cfg.staggered:
                        meta["last_weights"] = wanted
                before_cash = cash
                positions, cash = self.broker.positions(), self.broker.cash_usd()
                if cash < 0:  # see above; the fills still settle from the share counts
                    cash = before_cash - sum(o["value"] if o["side"] == "buy" else -o["value"]
                                             for o in orders)
                self._reconcile(positions)
                self._settle_fees(meta, cash)

        equity = cash + sum(p["qty"] * prices.get(s, 0.0) for s, p in positions.items())
        self._equity_row({"date": str(last.date()), "equity": round(equity, 2),
                          "cash": round(cash, 2),
                          "benchmark_close": round(prices.get(BENCHMARK, float("nan")), 4),
                          **({"usdkrw": self.fx} if self.fx else {})}, flow=flow)
        if not self._pending_orders():  # the baseline for spotting the next deposit or withdrawal
            meta["seen"] = {"time": now.isoformat(timespec="seconds"), "cash": round(cash, 2),
                            "qty": {s: p["qty"] for s, p in positions.items() if p["qty"]}}
        withdraw = self._withdraw_ready(cash, paused)
        if execute:
            meta["last_run"] = str(today.date())
        self._write_atomic("meta.json", meta)
        preview = None
        try:
            preview = preview_rebalance(close, today, self.cfg, positions, cash, prices,
                                        ran_today=meta.get("last_run") == str(today.date()))
        except Exception as e:  # noqa: BLE001 - a preview must never stop a run
            alerts.append({"level": "info", "message": f"다음 리밸런싱 미리보기를 계산하지 못했습니다: {str(e)[:120]}"})
        backtest = None
        try:
            cost = self.cfg.model_cost_bps
            if cost is None:
                cost = 25.0 if self.mode == "kiwoom_mock" else 10.0
            backtest = shadow_backtest(close, self.cfg, self._equity_rows(), cost)
        except Exception as e:  # noqa: BLE001 - the comparison must never stop a run
            alerts.append({"level": "info", "message": f"백테스트 비교를 계산하지 못했습니다: {str(e)[:120]}"})
        return self._state(now, meta, alerts=alerts, positions=positions, cash=cash,
                           prices=prices, targets=targets, scores=scores, sig=sig, canary=canary, parts=parts,
                           backtest=backtest, preview=preview, withdraw=withdraw)

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

    def _rebalance_days(self, now, meta) -> dict:
        """The next order day not yet run, and whether today was one. Once
        tonight's order run is done, today's date is past (2026-10-15: the
        dashboard showed "확인 필요" after midnight for the finished 10/15 run)."""
        day = pd.Timestamp(now.date())
        nxt = next_rebalance(day, self.cfg)
        today = nxt if nxt["order_date"] == str(day.date()) else None
        if today and meta.get("last_run") == str(session_cutoff(now).date()):
            nxt = next_rebalance(day + mc.bday(), self.cfg)
        return {"next_rebalance": nxt, "rebalance_today": today}

    def _state(self, now, meta, alerts, positions=None, cash=None, prices=None,
               targets=None, scores=None, sig=None, canary=None, backtest=None, preview=None,
               parts=None, withdraw=None) -> dict:
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
            **self._money_in(meta),
            "benchmark_start": meta.get("benchmark_start"),
            "updated_at": now.isoformat(timespec="seconds"),
            "cash": round(cash, 2) if cash is not None else None,
            "positions": [{"symbol": s, "qty": p["qty"], "avg_price": round(p["avg_price"], 4),
                           "last_price": prices.get(s), "sleeve": sleeve_of.get(s)}
                          for s, p in positions.items()],
            "targets": targets or [],
            "last_rebalance": meta.get("last_rebalance"),
            **self._rebalance_days(now, meta),
            "signal": {"as_of": str(sig.date()) if sig is not None else None,
                       "lookback_days": max(self.cfg.lookbacks or (self.cfg.lookback,)),
                       "lookbacks": list(self.cfg.lookbacks or (self.cfg.lookback,)),
                       "scores": scores or []},
            "alerts": alerts,
        }
        if getattr(self, "closes", None):
            state["closes"] = self.closes
        if self.cfg.canary and canary is not None:
            state["signal"]["canary"] = canary
        if parts:
            state["signal"]["tranches"] = parts
        if backtest is None:  # early exits (pause, stale data) keep the last comparison
            backtest = self._read("state.json").get("backtest")
        if backtest:
            state["backtest"] = backtest
        fx = {"usdkrw": self.fx, "as_of": now.isoformat(timespec="seconds")} if getattr(self, "fx", None) \
            else self._read("state.json").get("fx")
        if fx:
            state["fx"] = fx
        if getattr(self, "planned", None) is not None:
            state["planned_orders"] = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items()}
                                       for o in self.planned]
        if preview is None:
            preview = self._read("state.json").get("preview")
        if preview:
            state["preview"] = preview
        if withdraw is None:
            withdraw = self.withdraw_request()
        if withdraw:
            state["withdraw"] = withdraw
        self._write_atomic("state.json", state)
        return state
