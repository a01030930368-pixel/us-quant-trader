"""Temporary: what the nightly run sees from Yahoo right now."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.data import load_prices  # noqa: E402
from quant.strategies import momentum_13612  # noqa: E402
from quant.trader import runner as rn  # noqa: E402

cfg = rn.TraderConfig(canary=("EEM",), tranches=4)
tickers = sorted(set(rn.DATA_TICKERS) | set(cfg.canary))
raw = load_prices(tickers, "2009-01-01", use_cache=False)
print("raw tail\n", raw.tail(4).round(2).to_string())
close = raw.dropna()
now = datetime.now(timezone.utc)
today = rn.session_cutoff(now)
print("now", now, "today", today.date(), "last row", close.index[-1].date())
print("rows in Oct:", [d.day for d in close.index if d >= pd.Timestamp("2026-10-01")])
rows, last = rn.staggered_targets(close, today, cfg)
print("targets as of", last.date(), {r["symbol"]: r["weight"] for r in rows})
for p in rn.tranche_detail(close, today, cfg):
    print("tranche", p)
mom = momentum_13612(close[["EEM"]])
print("EEM 13612W\n", mom.tail(6).round(4).to_string())
for d in ("2026-10-20", "2026-10-21"):
    sub = close.loc[:d]
    print(d, "EEM mom", round(float(momentum_13612(sub[["EEM"]]).iloc[-1, 0]), 4))
