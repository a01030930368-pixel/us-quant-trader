"""Post-earnings-announcement drift (PEAD): buy after a big earnings surprise, hold ~3 months.

Stocks whose quarterly earnings beat expectations by a wide margin keep
drifting up for weeks after the report (Ball & Brown 1968, Foster, Olsen &
Shevlin 1984, Bernard & Thomas 1989). It is one of the most often confirmed
patterns in finance, and unlike the legend rules it needs no chart reading.

The surprise is "standardized unexpected earnings" (SUE) [원전 Foster et al.]:
    (EPS this quarter - EPS same quarter a year ago)
    / standard deviation of that year-over-year change over the previous
      8 quarters (at least 4).
With no analyst forecasts in free data, "expected" means "same as a year ago".

Rules, fixed before the first run (doc chapter 19):
- The signal day is the first trading day after the 10-Q or 10-K filing. The
  free SEC data has only the filing date, which is often days or weeks after
  the earnings press release, so part of the drift is already gone [근사].
- Buy at the next open when SUE >= 2 [근사: roughly the top 10-15% of
  surprises, the "top decile" of the papers], biggest surprise first.
- 10 positions of 10% each [해석: the papers hold many stocks].
- Sell at the open after 60 trading days [원전: the drift is concentrated in
  the 60 days after the report].
- No new buys while the S&P 500 is below its 200-day average (loss control,
  not in the papers).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .fundamentals import EPS_TAGS, quarterly
from .strategies import Strategy

SUE_MIN = 2.0
HOLD_DAYS = 60


def sue_events(q: pd.DataFrame, history: int = 8, min_history: int = 4) -> pd.DataFrame:
    """SUE per quarter from one value per quarter (columns end, filed, val)."""
    if q.empty:
        return pd.DataFrame(columns=["end", "filed", "sue"])
    q = q.sort_values("end").reset_index(drop=True)
    change = []
    for _, row in q.iterrows():
        prev = q[(row["end"] - q["end"]).dt.days.between(350, 380)]
        change.append(row["val"] - prev["val"].iloc[0] if len(prev) else np.nan)
    q["change"] = change
    sue = []
    for k in range(len(q)):
        past = q["change"].iloc[max(0, k - history):k].dropna()
        sd = past.std() if len(past) >= min_history else np.nan
        sue.append(q["change"].iloc[k] / sd if sd and sd > 0 else np.nan)
    return pd.DataFrame({"end": q["end"], "filed": q["filed"], "sue": sue})


def sue_table(facts: dict[str, dict], index: pd.DatetimeIndex) -> tuple[pd.DataFrame, int]:
    """SUE on each stock's signal day (the trading day after filing), NaN on other days.

    Also returns how many stocks had at least one SUE.
    """
    out = pd.DataFrame(np.nan, index=index, columns=list(facts))
    covered = 0
    for t, f in facts.items():
        ev = sue_events(quarterly(EPS_TAGS, f, "USD/shares")).dropna()
        covered += int(len(ev) > 0)
        # Two quarters filed together (a late 10-K): keep the newest quarter.
        ev = ev.sort_values(["filed", "end"]).drop_duplicates("filed", keep="last")
        pos = index.searchsorted(ev["filed"].values, side="right")     # next trading day
        for p, s in zip(pos, ev["sue"]):
            if p < len(index):
                out.iat[p, out.columns.get_loc(t)] = s
    return out, covered


class PEAD(Strategy):
    name = "pead"
    label = "실적 서프라이즈 후 추세"
    description = ("분기 실적 공시 다음 날, EPS가 1년 전 같은 분기보다 평소 변동폭의 2배 이상 "
                   "늘었으면(SUE 2 이상) 다음 날 시가에 사서 60거래일 뒤 팝니다. 최대 10종목, "
                   "한 종목 10%. S&P500이 200일 평균 아래면 새로 사지 않습니다.")
    max_positions = 10

    def _prepare(self) -> None:
        self.sue = self.extra["SUE"].reindex(index=self.c.index)

    def prepare(self, d: dict[str, pd.DataFrame], market: str = "SPY"):
        self.extra = d
        return super().prepare(d, market)

    def signal(self, i: int) -> pd.Series:
        s = self.sue.iloc[i].reindex(self.tradable)
        return s[s >= SUE_MIN]

    def candidates(self, i: int) -> list[str]:
        if not self.market_ok.iloc[i]:
            return []
        return list(self.signal(i).sort_values(ascending=False).index)

    def exit_reason(self, pos: dict, i: int) -> str | None:
        if i - pos["entry_i"] + 1 >= HOLD_DAYS:
            return "60거래일 보유 완료"
        return None


class PEADReaction(PEAD):
    """Only surprises the market also liked: the stock beat the S&P 500 over the last 20 days."""
    name = "pead_reaction"
    label = "실적 서프라이즈 + 주가 반응 확인"
    description = ("위 규칙에, 공시 다음 날까지 20거래일 동안 주가가 S&P500보다 더 올랐을 때만 "
                   "삽니다(시장도 좋은 실적으로 받아들였는지 확인).")
    lookback = 20                     # [해석] covers the press release before the filing

    def _prepare(self) -> None:
        super()._prepare()
        r = self.c / self.c.shift(self.lookback) - 1
        self.excess = r.sub(r[self.market], axis=0)

    def signal(self, i: int) -> pd.Series:
        s = super().signal(i)
        return s[self.excess.iloc[i].reindex(s.index) > 0]


class PEADReactionStop(PEADReaction):
    name = "pead_reaction_stop"
    label = "실적 서프라이즈 + 반응 + 손절 -15%"
    description = "위 규칙에, 산 값보다 15% 내리면 바로 파는 손절을 더합니다."
    stop_pct = 0.15                   # [해석] loss control, not in the papers

    def initial_stop(self, ticker: str, i: int, price: float) -> float:
        return price * (1 - self.stop_pct)


PEAD_SET = [PEAD, PEADReaction, PEADReactionStop]
