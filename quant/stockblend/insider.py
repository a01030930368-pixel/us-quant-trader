"""Insider buying: follow company officers and directors who buy their own stock.

Officers and directors must report every trade in their company's stock on
SEC Form 4 within two business days. Sales happen for many reasons (taxes,
diversification), but an open-market purchase with their own money is
usually a vote of confidence, and such purchases have been followed by
above-market returns (Lakonishok & Lee 2001; Cohen, Malloy & Pomorski 2012).
The edge is larger in small companies and has faded in large caps.

The SEC publishes every Form 4 since 2006 as free quarterly files ("Insider
Transactions Data Sets"), so this can be tested point in time: a purchase is
usable from the trading day after its filing date.

Rules, fixed before the first run (doc chapter 24). A and B copy the best
public test we found (Quiver Quantitative, "Testing Five Trading Strategies
Built on Insider Buying", S&P 500, 2021-2026: only B beat holding the index).
A. Each month end, add up the open-market purchases (code P) by officers and
   directors filed in the last 63 trading days; hold the 10 stocks with the
   largest dollar amount, 10% each. Sell at the next month end when a stock
   is no longer in the top 10 [원전].
B. A, but only stocks above their 200-day average [원전].
C. Event version: buy at the next open after a day when officers and directors
   filed purchases of at least $100,000 in total, if the stock is above its
   200-day average; hold 60 trading days; 10 positions, biggest purchase
   first [해석: the returns in the studies come in the weeks after filing].
Ten-percent owners who are not officers or directors (mostly funds) are left
out [원전 Cohen et al.: the signal is in insiders who run the company].
"""
from __future__ import annotations

import io
import time
import zipfile

import numpy as np
import pandas as pd

from .fundamentals import CACHE_DIR, USER_AGENT
from .quality import month_ends
from .strategies import Strategy

URLS = ["https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
        "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{q}_form345.zip"]
TOP = 10
LOOKBACK = 63
EVENT_MIN = 100_000
HOLD_DAYS = 60


def _download(q: str) -> bytes | None:
    import requests

    path = CACHE_DIR / "insider" / f"{q}_form345.zip"
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    for url in URLS:
        for attempt in range(4):
            try:
                r = requests.get(url.format(q=q), headers={"User-Agent": USER_AGENT}, timeout=120)
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                path.write_bytes(r.content)
                time.sleep(0.2)
                return r.content
            if r.status_code == 404:
                break
            time.sleep(2 ** attempt)
    print(f"SEC 내부자 거래 파일을 받지 못해 건너뜁니다: {q}")
    return None


def _table(z: zipfile.ZipFile, name: str, cols: list[str]) -> pd.DataFrame:
    member = next(n for n in z.namelist() if n.upper().endswith(name))
    want = set(cols)
    return pd.read_csv(io.BytesIO(z.read(member)), sep="\t", dtype=str, usecols=lambda c: c in want,
                       quoting=3, on_bad_lines="skip")


def purchases_from_zip(raw: bytes) -> pd.DataFrame:
    """Open-market purchases by officers and directors: columns ticker, filed, value."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    sub = _table(z, "SUBMISSION.TSV", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE",
                                       "FORM_TYPE", "ISSUERTRADINGSYMBOL"])
    trans = _table(z, "NONDERIV_TRANS.TSV", ["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES",
                                             "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
    owner = _table(z, "REPORTINGOWNER.TSV", ["ACCESSION_NUMBER", "RPTOWNER_RELATIONSHIP"])
    form = "DOCUMENT_TYPE" if "DOCUMENT_TYPE" in sub else "FORM_TYPE"
    sub = sub[sub[form].str.strip() == "4"]                       # amendments (4/A) would count twice
    insiders = owner["RPTOWNER_RELATIONSHIP"].fillna("").str.upper().str.contains("OFFICER|DIRECTOR")
    insiders = set(owner.loc[insiders, "ACCESSION_NUMBER"])
    t = trans[(trans["TRANS_CODE"] == "P") & (trans["TRANS_ACQUIRED_DISP_CD"] == "A")
              & trans["ACCESSION_NUMBER"].isin(insiders)].copy()
    t["value"] = pd.to_numeric(t["TRANS_SHARES"], errors="coerce") * pd.to_numeric(
        t["TRANS_PRICEPERSHARE"], errors="coerce")
    t = t.merge(sub, on="ACCESSION_NUMBER")
    out = pd.DataFrame({
        "ticker": t["ISSUERTRADINGSYMBOL"].str.strip().str.upper().str.replace(".", "-", regex=False),
        "filed": pd.to_datetime(t["FILING_DATE"], format="%d-%b-%Y", errors="coerce"),
        "value": t["value"]})
    return out.dropna()


def load_purchases(first_year: int, last: pd.Timestamp) -> pd.DataFrame:
    """All officer/director open-market purchases filed from first_year to `last`."""
    frames = []
    for year in range(first_year, last.year + 1):
        for k in range(1, 5):
            if pd.Timestamp(year, 3 * k - 2, 1) > last:
                break
            raw = _download(f"{year}q{k}")
            if raw:
                frames.append(purchases_from_zip(raw))
    if not frames:
        return pd.DataFrame(columns=["ticker", "filed", "value"])
    return pd.concat(frames, ignore_index=True)


def purchase_table(p: pd.DataFrame, index: pd.DatetimeIndex, tickers: list[str]) -> pd.DataFrame:
    """Dollars bought per stock, placed on the trading day after the filing date."""
    p = p[p["ticker"].isin(tickers) & (p["value"] > 0)]
    pos = index.searchsorted(p["filed"].values, side="right")
    keep = pos < len(index)
    if not keep.any():
        return pd.DataFrame(0.0, index=index, columns=tickers)
    daily = (pd.DataFrame({"day": index[pos[keep]], "ticker": p["ticker"].values[keep],
                           "value": p["value"].values[keep]})
             .groupby(["day", "ticker"])["value"].sum().unstack())
    return daily.reindex(index=index, columns=tickers).fillna(0.0)


class InsiderDollar(Strategy):
    name = "insider_dollar"
    label = "내부자 매수 금액 상위 10"
    description = ("매달 말, 최근 3개월(63거래일) 동안 임원·이사가 장내에서 산 금액이 가장 큰 10개 "
                   "종목을 보유합니다. 다음 달 말에 10위 밖이면 팝니다. 한 종목 10%.")
    max_positions = TOP

    def prepare(self, d, market: str = "SPY"):
        self.buys_all = d["IBUY"]
        return super().prepare(d, market)

    def _prepare(self) -> None:
        buys = self.buys_all.reindex(index=self.c.index, columns=self.tradable).fillna(0.0)
        self.total = buys.rolling(LOOKBACK, min_periods=1).sum()
        self.month_end = month_ends(self.c.index)

    def eligible(self, i: int) -> pd.Series:
        return self.c.iloc[i].reindex(self.tradable).notna()

    def ranked(self, i: int) -> list[str]:
        s = self.total.iloc[i]
        s = s[self.eligible(i) & (s > 0)]
        return list(s.sort_values(ascending=False).index[:TOP])

    def candidates(self, i: int) -> list[str]:
        return self.ranked(i) if self.month_end[i] else []

    def exit_reason(self, pos: dict, i: int) -> str | None:
        if self.month_end[i] and pos["ticker"] not in self.ranked(i):
            return "내부자 매수 순위 밖"
        return None


class InsiderDollarTrend(InsiderDollar):
    name = "insider_dollar_trend"
    label = "내부자 매수 상위 10 + 200일선"
    description = "위 규칙에, 주가가 200일 평균 위인 종목만 고릅니다."

    def eligible(self, i: int) -> pd.Series:
        return (self.c.iloc[i] > self.sma200.iloc[i]).reindex(self.tradable).fillna(False)


class InsiderEvent(InsiderDollar):
    name = "insider_event"
    label = "내부자 10만 달러 이상 매수 공시 후 60일"
    description = ("임원·이사의 장내 매수 공시가 하루 합계 10만 달러 이상이고 주가가 200일 평균 위면 "
                   "다음 날 시가에 삽니다. 60거래일 보유, 최대 10종목, 매수 금액이 큰 순서.")

    def _prepare(self) -> None:
        self.buys = self.buys_all.reindex(index=self.c.index, columns=self.tradable).fillna(0.0)

    def candidates(self, i: int) -> list[str]:
        b = self.buys.iloc[i]
        up = (self.c.iloc[i] > self.sma200.iloc[i]).reindex(self.tradable).fillna(False)
        return list(b[(b >= EVENT_MIN) & up].sort_values(ascending=False).index)

    def exit_reason(self, pos: dict, i: int) -> str | None:
        if i - pos["entry_i"] + 1 >= HOLD_DAYS:
            return "60거래일 보유 완료"
        return None


INSIDER = [InsiderDollar, InsiderDollarTrend, InsiderEvent]
