"""Long-hold quality strategies: own the most profitable companies, trade rarely.

With a 0.25% commission per side, only strategies that hold for months
survive (doc chapter 21). The most robust long-hold stock-picking evidence
we found (doc chapter 22) is gross profitability, Novy-Marx (2013):
    gross profit (sales - cost of goods) / total assets.
Companies high on this measure have beaten low ones, and the gap was larger
after the paper was published (2007-2023) than before (Novy-Marx & co.,
NBER w33601, 2025). Value screens such as Greenblatt's Magic Formula have
trailed the S&P 500 since about 2015, and need market caps we cannot build
cleanly from free data, so they are not tested.

Rules, fixed before the first run:
A. 20 stocks with the highest gross profitability; checked at each month end,
   a holding is sold when it drops out of the top 40 (keeps trading rare).
B. A, but buy only stocks above their 200-day average and sell when one
   closes below it (loss control).
C. Quality + earnings surprise: the PEAD rules (chapter 19), but only for
   stocks in the top 30% by gross profitability.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .fundamentals import ASSET_TAGS, COGS_TAGS, GROSS_TAGS, SALES_TAGS, quarterly
from .pead import PEAD
from .strategies import Strategy

TOP = 20                  # [해석] the papers hold deciles; 20 names is a practical account size
KEEP = 40                 # [해석] sell only when it falls out of the top 40
STALE_DAYS = 200          # no new report for this long: treat the number as unknown


def instant(tags: list[str], facts: dict, unit: str = "USD") -> pd.DataFrame:
    """Point-in-time balance-sheet values (columns end, filed, val), first filed per date."""
    rows = []
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        for u, items in gaap.get(tag, {}).get("units", {}).items():
            if u != unit:
                continue
            for it in items:
                if "start" not in it and it.get("form", "").startswith(("10-Q", "10-K")):
                    rows.append({"end": it["end"], "filed": it["filed"], "val": it["val"]})
    if not rows:
        return pd.DataFrame(columns=["end", "filed", "val"])
    df = pd.DataFrame(rows)
    df["end"], df["filed"] = pd.to_datetime(df["end"]), pd.to_datetime(df["filed"])
    return df.sort_values("filed").drop_duplicates("end", keep="first")


def quarterly_gross(facts: dict) -> pd.DataFrame:
    """Gross profit per quarter: the reported figure, else sales minus cost of goods."""
    gp = quarterly(GROSS_TAGS, facts, "USD")
    if len(gp):
        return gp
    sales, cogs = quarterly(SALES_TAGS, facts, "USD"), quarterly(COGS_TAGS, facts, "USD")
    if sales.empty or cogs.empty:
        return pd.DataFrame(columns=["end", "filed", "val"])
    m = sales.merge(cogs, on="end", suffixes=("_s", "_c"))
    return pd.DataFrame({"end": m["end"], "filed": m[["filed_s", "filed_c"]].max(axis=1),
                         "val": m["val_s"] - m["val_c"]})


def profitability_events(facts: dict) -> pd.DataFrame:
    """Trailing 4-quarter gross profit / total assets, with the date it became known."""
    gp = quarterly_gross(facts).sort_values("end").reset_index(drop=True)
    assets = instant(ASSET_TAGS, facts)
    if len(gp) < 4 or assets.empty:
        return pd.DataFrame(columns=["known", "gpa"])
    rows = []
    for k in range(3, len(gp)):
        four = gp.iloc[k - 3:k + 1]
        if not 250 <= (four["end"].iloc[-1] - four["end"].iloc[0]).days <= 300:
            continue                                   # a missing quarter in between
        a = assets[(assets["end"] - four["end"].iloc[-1]).abs() <= pd.Timedelta(days=7)]
        if a.empty or a["val"].iloc[0] <= 0:
            continue
        rows.append({"known": max(four["filed"].max(), a["filed"].iloc[0]),
                     "gpa": four["val"].sum() / a["val"].iloc[0]})
    return pd.DataFrame(rows, columns=["known", "gpa"])


def profitability_table(facts: dict[str, dict], index: pd.DatetimeIndex) -> tuple[pd.DataFrame, int]:
    """Daily gross profitability per stock, usable from the trading day after it was filed."""
    out = pd.DataFrame(np.nan, index=index, columns=list(facts))
    covered = 0
    for t, f in facts.items():
        ev = profitability_events(f)
        if ev.empty:
            continue
        covered += 1
        ev = ev.sort_values("known").drop_duplicates("known", keep="last")
        pos = index.searchsorted(ev["known"].values, side="right")
        col = pd.Series(np.nan, index=index)
        known = pd.Series(pd.NaT, index=index)
        for p, (k, g) in zip(pos, ev[["known", "gpa"]].itertuples(index=False)):
            if p < len(index):
                col.iloc[p], known.iloc[p] = g, k
        col, known = col.ffill(), known.ffill()
        out[t] = col.where((index.to_series() - known).dt.days <= STALE_DAYS)
    return out, covered


def month_ends(idx: pd.DatetimeIndex) -> np.ndarray:
    m = pd.Series(idx.to_period("M"), index=idx)
    out = (m != m.shift(-1)).to_numpy().copy()
    out[-1] = False                                    # the month may not be over yet
    return out


class GrossProfitability(Strategy):
    name = "gross_profit"
    label = "총이익률 상위 20종목"
    description = ("매달 말, (매출 − 매출원가) ÷ 총자산이 가장 높은 20개 회사를 보유합니다. 순위가 40위 "
                   "밖으로 밀리면 다음 달 초에 팝니다. 한 종목 5%. 실적은 공시 다음 날부터 씁니다.")
    max_positions = TOP

    def prepare(self, d, market: str = "SPY"):
        self.gpa_all = d["GPA"]
        return super().prepare(d, market)

    def _prepare(self) -> None:
        self.gpa = self.gpa_all.reindex(index=self.c.index, columns=self.tradable)
        self.month_end = month_ends(self.c.index)

    def eligible(self, i: int) -> pd.Series:
        return self.c.iloc[i].reindex(self.tradable).notna()

    def ranked(self, i: int) -> pd.Series:
        g = self.gpa.iloc[i]
        return g[self.eligible(i) & g.notna()].sort_values(ascending=False)

    def candidates(self, i: int) -> list[str]:
        if not self.month_end[i]:
            return []
        return list(self.ranked(i).index[:TOP])

    def exit_reason(self, pos: dict, i: int) -> str | None:
        if self.month_end[i] and pos["ticker"] not in self.ranked(i).index[:KEEP]:
            return "총이익률 순위 하락"
        return None


class GrossProfitabilityTrend(GrossProfitability):
    name = "gross_profit_trend"
    label = "총이익률 상위 20 + 200일선"
    description = ("위 규칙에, 주가가 200일 평균 위인 종목만 사고, 200일 평균 아래로 마감하면 다음 날 "
                   "시가에 팝니다(손실 관리).")

    def eligible(self, i: int) -> pd.Series:
        return (self.c.iloc[i] > self.sma200.iloc[i]).reindex(self.tradable).fillna(False)

    def exit_reason(self, pos: dict, i: int) -> str | None:
        t = pos["ticker"]
        if self.c[t].iloc[i] < self.sma200[t].iloc[i]:
            return "200일 평균 이탈"
        return super().exit_reason(pos, i)


class QualityPEAD(PEAD):
    name = "quality_pead"
    label = "실적 서프라이즈 + 총이익률 상위 30%"
    description = ("19장의 실적 서프라이즈 규칙(SUE 2 이상, 60거래일 보유, 10종목)에, 그날 총이익률이 "
                   "상위 30%인 회사만 삽니다.")

    def prepare(self, d, market: str = "SPY"):
        self.gpa_all = d["GPA"]
        return super().prepare(d, market)

    def _prepare(self) -> None:
        super()._prepare()
        self.gpa = self.gpa_all.reindex(index=self.c.index)

    def signal(self, i: int) -> pd.Series:
        s = super().signal(i)
        g = self.gpa.iloc[i]
        cut = g.quantile(0.7)
        return s[g.reindex(s.index) >= cut]


QUALITY = [GrossProfitability, GrossProfitabilityTrend, QualityPEAD]
