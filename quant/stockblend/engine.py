"""Trade-by-trade swing backtest.

Timeline for each trading day i, which keeps the strategy from seeing the future:

1. Open:   sell what yesterday's close said to sell (all or part), then buy
           yesterday's picks at the open, or at a buy-stop price during the
           day if the strategy uses one (filled only if the high reaches it).
           Each buy gets the strategy's weight (at most 1/max_positions of the
           account, the per-stock limit).
2. During the day: a stop loss fills at the stop price if the low touches it,
           or at the open if the stock gaps below the stop. Then a profit
           target (if the strategy has one) fills at the target if the high
           reaches it, or at the open on a gap above it. Stops are checked
           first, the cautious assumption when both happen on one day.
3. Close:  value the account, move trailing stops, and decide tomorrow's
           sells and buys from today's close.

Every buy and sell pays `cost_bps` (commission + slippage + FX spread).
Shares are fractional to keep the math simple; with whole shares a small
account tracks this a little less closely.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .strategies import Strategy


@dataclass
class Result:
    name: str
    equity: pd.Series                       # account value, starts at 1.0
    trades: pd.DataFrame                    # one row per closed trade
    exposure: pd.Series                     # share of the account in stocks each day
    open_positions: list[dict] = field(default_factory=list)

    @property
    def returns(self) -> pd.Series:
        return self.equity.pct_change().fillna(0.0)


def run(strategy: Strategy, cost_bps: float = 20.0, start: str | None = None) -> Result:
    o, h, l, c = strategy.o, strategy.h, strategy.l, strategy.c
    dates = c.index
    first = 0 if start is None else int(dates.searchsorted(pd.Timestamp(start)))
    cost = cost_bps / 10_000
    n_max = strategy.max_positions

    cash = 1.0
    positions: dict[str, dict] = {}
    pending_sells: dict[str, tuple[str, float]] = {}
    pending_buys: list[str] = []
    trades: list[dict] = []
    equity, exposure = [], []
    last_close = c.ffill()

    def close_trade(t: str, i: int, price: float, reason: str, frac: float = 1.0) -> None:
        """Sell `frac` of the position (1.0 = all of it)."""
        nonlocal cash
        pos = positions[t]
        shares = pos["shares"] if frac >= 1 else pos["shares"] * frac
        part = shares / pos["initial_shares"]
        cash += shares * price * (1 - cost)
        if frac >= 1:
            positions.pop(t)
        else:
            pos["shares"] -= shares
        net = price * (1 - cost) / (pos["entry_price"] * (1 + cost)) - 1
        trades.append({"ticker": t, "entry_date": dates[pos["entry_i"]].date(),
                       "entry_price": pos["entry_price"], "exit_date": dates[i].date(),
                       "exit_price": price, "return": net,
                       "days": i - pos["entry_i"] + 1, "reason": reason, "part": part,
                       "weight": pos["weight"]})

    prev_equity = 1.0
    for i in range(first, len(dates)):
        # 1. Open: sells first so their cash can fund today's buys.
        for t, (reason, frac) in list(pending_sells.items()):
            if t in positions:
                px = o[t].iloc[i]
                close_trade(t, i, px if pd.notna(px) else last_close[t].iloc[i], reason, frac)
        pending_sells.clear()
        for t in pending_buys:
            px = o[t].iloc[i]
            if len(positions) >= n_max or t in positions or pd.isna(px) or px <= 0:
                continue
            budget = prev_equity * strategy.weight(t, i - 1)
            spend = min(budget, cash / (1 + cost))
            if spend < budget * 0.5:          # not enough cash left for a real slot
                break
            px = strategy.entry_fill(t, i, px, h[t].iloc[i])
            if px is None or not strategy.accept_entry(t, i, px):
                continue
            positions[t] = {"ticker": t, "shares": spend / px, "initial_shares": spend / px,
                            "entry_price": px, "entry_i": i, "high_close": px, "high_max": px,
                            "weight": spend / prev_equity,
                            "stop": strategy.initial_stop(t, i, px)}
            cash -= spend * (1 + cost)
        pending_buys = []

        # 2. Intraday stop losses (not on the entry day's open itself).
        for t in list(positions):
            stop = positions[t]["stop"]
            lo, op = l[t].iloc[i], o[t].iloc[i]
            if stop is None or pd.isna(lo):
                continue
            if positions[t]["entry_i"] < i and op <= stop:
                close_trade(t, i, op, "손절 (갭 하락)")
            elif lo <= stop:
                close_trade(t, i, stop, "손절")

        # 2b. Intraday profit targets (full or partial sells).
        for t in list(positions):
            hi, op = h[t].iloc[i], o[t].iloc[i]
            if pd.isna(hi):
                continue
            pos = positions[t]
            pos["high_max"] = max(pos["high_max"], hi)
            tp = strategy.take_profit(pos, i)
            if tp is None:
                continue
            target, frac, reason = tp
            if hi >= target:
                fill = op if pos["entry_i"] < i and op > target else target
                close_trade(t, i, fill, reason, frac)

        # 3. Close: value the account, then plan tomorrow.
        value = cash + sum(p["shares"] * last_close[t].iloc[i] for t, p in positions.items())
        equity.append(value)
        exposure.append(1 - cash / value if value > 0 else 0.0)
        prev_equity = value
        if i == len(dates) - 1:
            break
        for t, pos in positions.items():
            px = c[t].iloc[i]
            if pd.notna(px):
                pos["high_close"] = max(pos["high_close"], px)
            pos["stop"] = strategy.update_stop(pos, i)
            reason = strategy.exit_reason(pos, i)
            if reason:
                pending_sells[t] = reason if isinstance(reason, tuple) else (reason, 1.0)
        full_sells = sum(1 for _, frac in pending_sells.values() if frac >= 1)
        free = n_max - (len(positions) - full_sells)
        if free > 0:
            picks = [t for t in strategy.candidates(i) if t not in positions]
            # Limit-order strategies place an order for every signal; most never fill.
            pending_buys = picks if getattr(strategy, "order_all", False) else picks[:free]

    eq = pd.Series(equity, index=dates[first:first + len(equity)], name=strategy.name)
    exp = pd.Series(exposure, index=eq.index)
    cols = ["ticker", "entry_date", "entry_price", "exit_date", "exit_price", "return", "days",
            "reason", "part", "weight"]
    open_pos = [{"ticker": t, "entry_date": dates[p["entry_i"]].date(),
                 "entry_price": p["entry_price"], "stop": p["stop"]} for t, p in positions.items()]
    return Result(strategy.name, eq, pd.DataFrame(trades, columns=cols), exp, open_pos)
