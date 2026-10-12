"""Quarterly earnings (EPS) and sales from the SEC's free XBRL data, point in time.

Every US-listed company files its quarterly (10-Q) and annual (10-K) reports
with the SEC, which publishes the numbers as JSON at data.sec.gov. Each number
carries the date it was filed, so the backtest can use a quarter's results
only from the day after the report became public, never earlier.

Rules for building one value per quarter:
- The first filed value wins. Later restatements were not known at the time.
- Most companies report only the full year in the 10-K, so the fourth quarter
  is the full year minus the first three quarters [근사 for EPS: share counts
  differ a little between quarters].
- Banks and some other companies use revenue tags we do not read; they have
  no sales numbers and never pass a sales condition.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path("data_cache") / "sec"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# The SEC asks every client to name itself; set SEC_USER_AGENT to override.
USER_AGENT = os.environ.get("SEC_USER_AGENT",
                            "us-quant-day-or-swing-trader backtest research bot")

EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
SALES_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
              "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
              "SalesRevenueGoodsNet"]
GROSS_TAGS = ["GrossProfit"]
COGS_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]
ASSET_TAGS = ["Assets"]
KEEP_TAGS = EPS_TAGS + SALES_TAGS + GROSS_TAGS + COGS_TAGS + ASSET_TAGS


def _get(url: str) -> dict:
    import requests

    for attempt in range(5):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(2 ** attempt)                   # the SEC server sometimes stalls: try again
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return {}
        time.sleep(2 ** attempt)
    print(f"SEC 데이터를 받지 못해 건너뜁니다: {url}")
    return {}


def _facts(tag_list: list[str], facts: dict, unit: str) -> pd.DataFrame:
    """All filed values of the first tags that exist, one row per (start, end, filed)."""
    rows = []
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for rank, tag in enumerate(tag_list):
        for u, items in gaap.get(tag, {}).get("units", {}).items():
            if u != unit:
                continue
            for it in items:
                if "start" in it and it.get("form", "").startswith(("10-Q", "10-K")):
                    rows.append({"start": it["start"], "end": it["end"], "val": it["val"],
                                 "filed": it["filed"], "rank": rank})
    if not rows:
        return pd.DataFrame(columns=["start", "end", "val", "filed", "rank"])
    df = pd.DataFrame(rows)
    for col in ("start", "end", "filed"):
        df[col] = pd.to_datetime(df[col])
    return df


def quarterly(tag_list: list[str], facts: dict, unit: str) -> pd.DataFrame:
    """One value per fiscal quarter: columns end, filed, val (first filed, Q4 derived)."""
    df = _facts(tag_list, facts, unit)
    if df.empty:
        return pd.DataFrame(columns=["end", "filed", "val"])
    days = (df["end"] - df["start"]).dt.days
    # Earliest filing per period; among tags filed the same day prefer the earlier tag.
    first = (df.sort_values(["filed", "rank"])
               .assign(days=days)
               .drop_duplicates(["start", "end"], keep="first"))
    q = first[first["days"].between(80, 100)][["start", "end", "filed", "val"]]
    years = first[first["days"].between(350, 380)]
    derived = []
    for _, y in years.iterrows():
        inside = q[(q["start"] >= y["start"] - pd.Timedelta(days=7)) & (q["end"] < y["end"] - pd.Timedelta(days=20))]
        if len(inside) != 3 or (q["end"].sub(y["end"]).abs() <= pd.Timedelta(days=7)).any():
            continue
        derived.append({"start": inside["end"].max() + pd.Timedelta(days=1), "end": y["end"],
                        "filed": max(y["filed"], inside["filed"].max()),
                        "val": y["val"] - inside["val"].sum()})
    out = pd.concat([q, pd.DataFrame(derived)], ignore_index=True) if derived else q
    return out.sort_values("end").drop_duplicates("end", keep="first")[["end", "filed", "val"]]


def yoy_growth(q: pd.DataFrame) -> pd.DataFrame:
    """Growth of each quarter over the same quarter a year earlier (columns end, filed, growth).

    Undefined (NaN) when the year-earlier value is zero or negative, as O'Neil
    ignores growth "from a loss".
    """
    if q.empty:
        return pd.DataFrame(columns=["end", "filed", "growth"])
    q = q.sort_values("end").reset_index(drop=True)
    growth = []
    for _, row in q.iterrows():
        prev = q[(row["end"] - q["end"]).dt.days.between(350, 380)]
        base = prev["val"].iloc[0] if len(prev) else np.nan
        growth.append(row["val"] / base - 1 if base > 0 else np.nan)
    return pd.DataFrame({"end": q["end"], "filed": q["filed"], "growth": growth})


def daily_flags(growth: pd.DataFrame, index: pd.DatetimeIndex, minimum: float,
                stale_days: int = 130) -> pd.Series:
    """True on days when the latest public quarter grew at least `minimum` year over year.

    A report filed on day d is first usable on the next trading day. With no
    new report for `stale_days` calendar days the flag turns off.
    """
    out = pd.Series(False, index=index)
    if growth.empty:
        return out
    g = growth.sort_values(["filed", "end"]).drop_duplicates("filed", keep="last")
    pos = index.searchsorted(g["filed"].values, side="right")      # next trading day
    val = pd.Series(np.nan, index=index)
    known = pd.Series(pd.NaT, index=index)
    for p, row in zip(pos, g.itertuples()):
        if p < len(index):
            val.iloc[p] = row.growth if pd.notna(row.growth) else -np.inf
            known.iloc[p] = row.filed
    val, known = val.ffill(), known.ffill()
    fresh = (index.to_series() - known).dt.days <= stale_days
    return (val >= minimum) & fresh


def _slim(facts: dict) -> dict:
    """Keep only the tags the strategies use (a full file can be several MB)."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    return {"facts": {"us-gaap": {k: gaap[k] for k in KEEP_TAGS if k in gaap}}}


def cik_of(use_cache: bool = True):
    """Function mapping a ticker to its SEC company number (CIK), or None."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cik_path = CACHE_DIR / "company_tickers.json"
    if use_cache and cik_path.exists():
        table = json.loads(cik_path.read_text())
    else:
        table = _get(TICKERS_URL)
        cik_path.write_text(json.dumps(table))
    cik = {row["ticker"].upper(): int(row["cik_str"]) for row in table.values()}
    return lambda t: cik.get(t.upper().replace("-", ".")) or cik.get(t.upper().replace("-", ""))


def load_facts(tickers: list[str], use_cache: bool = True) -> dict[str, dict]:
    """companyfacts JSON per ticker ({} when the SEC has none), cached on disk."""
    cik = cik_of(use_cache)
    out = {}
    for t in tickers:
        c = cik(t)
        if c is None:
            out[t] = {}
            continue
        path = CACHE_DIR / f"{c}.json"
        if use_cache and path.exists():
            out[t] = json.loads(path.read_text())
            continue
        out[t] = _slim(_get(FACTS_URL.format(cik=c)))
        path.write_text(json.dumps(out[t]))
        time.sleep(0.12)                     # SEC limit: at most 10 requests a second
    return out


def growth_flags(facts: dict[str, dict], index: pd.DatetimeIndex, eps_min: float,
                 sales_min: float) -> tuple[pd.DataFrame, dict[str, int]]:
    """Daily True/False per ticker for 'latest quarter EPS and sales both grew enough'.

    Also returns coverage counts for the report.
    """
    cols, cover = {}, {"eps": 0, "sales": 0}
    for t, f in facts.items():
        eps = yoy_growth(quarterly(EPS_TAGS, f, "USD/shares"))
        sales = yoy_growth(quarterly(SALES_TAGS, f, "USD"))
        cover["eps"] += int(eps["growth"].notna().any())
        cover["sales"] += int(sales["growth"].notna().any())
        cols[t] = daily_flags(eps, index, eps_min) & daily_flags(sales, index, sales_min)
    return pd.DataFrame(cols, index=index), cover
