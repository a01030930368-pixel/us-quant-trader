"""Swing strategy candidates.

Each strategy reads indicators computed once up front and answers four
questions for the engine, always using data up to the close of day i only:

    candidates(i)      stocks to buy at the next open, best first
    initial_stop(t, i, price)  stop price for a stock bought at day i's open
    update_stop(pos, i)        stop for tomorrow after day i's close (or None)
    exit_reason(pos, i)        why to sell at the next open (or None to keep)

Parameters are the commonly published defaults, not tuned on this data, so the
backtest is a fair test rather than a fitted curve.
"""
from __future__ import annotations

import pandas as pd

from .indicators import atr, rsi, sma


class Strategy:
    name = "base"
    label = ""
    description = ""
    max_positions = 5
    order_all = False                # True: send every candidate, the engine fills until slots run out

    def prepare(self, d: dict[str, pd.DataFrame], market: str = "SPY") -> "Strategy":
        self.o, self.h, self.l, self.c = d["Open"], d["High"], d["Low"], d["Close"]
        self.v = d.get("Volume")
        self.market = market
        self.sma200 = sma(self.c, 200)
        # Market filter: only open new positions while the S&P 500 is above
        # its 200-day average. Most big swing losses happen in bear markets.
        self.market_ok = (self.c[market] > self.sma200[market]).fillna(False)
        self.tradable = [t for t in self.c.columns if t != market]
        self._prepare()
        return self

    def _prepare(self) -> None:
        pass

    def candidates(self, i: int) -> list[str]:
        raise NotImplementedError

    def weight(self, ticker: str, i: int) -> float:
        """Share of the account for a new position, decided at day i's close."""
        return 1 / self.max_positions

    def entry_fill(self, ticker: str, i: int, open_: float, high: float) -> float | None:
        """Fill price for a buy decided at day i-1's close: the open by default.

        A buy-stop strategy returns its trigger price if day i's high reaches
        it (or the open if the stock opens above it), else None (no trade).
        """
        return open_

    def accept_entry(self, ticker: str, i: int, price: float) -> bool:
        """Last check at day i's open, e.g. skip a stock that gapped too far."""
        return True

    def initial_stop(self, ticker: str, i: int, price: float) -> float | None:
        return None

    def take_profit(self, pos: dict, i: int) -> tuple[float, float, str] | None:
        """(target price, share of the position to sell, reason) for day i, or None.

        Checked during day i after stops; pos["high_max"] already includes
        day i's high. A strategy may also change pos here (for example mark a
        partial sale as done, or move the stop for tomorrow).
        """
        return None

    def update_stop(self, pos: dict, i: int) -> float | None:
        return pos.get("stop")

    def exit_reason(self, pos: dict, i: int) -> str | tuple[str, float] | None:
        """Why to sell at the next open, or (reason, share to sell) for a partial sale."""
        return None


class Pullback(Strategy):
    """Buy a short, sharp dip in a stock that is in a long uptrend; sell on the bounce.

    Larry Connors' RSI(2) pullback: close above its 200-day average, 2-day RSI
    below 10. Sell when the close gets back above the 5-day average, after 10
    trading days at most, or at an 8% stop loss.
    """
    name = "pullback"
    label = "눌림목 매수"
    description = ("200일 평균 위에 있는 상승 종목이 2~3일 급하게 빠지면(RSI(2)<10) 사고, "
                   "5일 평균 위로 반등하면 팝니다. 최대 10거래일 보유, 손절 -8%.")
    stop_pct = 0.08
    max_days = 10

    def _prepare(self) -> None:
        self.rsi2 = rsi(self.c, 2)
        self.sma5 = sma(self.c, 5)

    def candidates(self, i: int) -> list[str]:
        if not self.market_ok.iloc[i]:
            return []
        c, r = self.c.iloc[i], self.rsi2.iloc[i]
        ok = (c > self.sma200.iloc[i]) & (r < 10)
        picks = r[ok].reindex(self.tradable).dropna()
        return list(picks.sort_values().index)

    def initial_stop(self, ticker: str, i: int, price: float) -> float:
        return price * (1 - self.stop_pct)

    def exit_reason(self, pos: dict, i: int) -> str | None:
        t = pos["ticker"]
        if self.c[t].iloc[i] > self.sma5[t].iloc[i]:
            return "반등 (5일 평균 회복)"
        if i - pos["entry_i"] + 1 >= self.max_days:
            return "보유기간 만료"
        return None


class Breakout(Strategy):
    """Buy a stock making a new 55-day high in an uptrend; ride it with a trailing stop.

    Classic Donchian / turtle style breakout. Ranked by 6-month momentum when
    more stocks break out than there are free slots. The stop starts 2 ATR
    below the entry and trails 3 ATR below the highest close since entry.
    """
    name = "breakout"
    label = "신고가 돌파"
    description = ("200일 평균 위에서 55일 신고가를 낸 종목을 사고, 고점 대비 3 ATR(평소 하루 "
                   "변동폭의 3배) 밀리면 팝니다. 처음 손절선은 매수가 - 2 ATR.")

    def _prepare(self) -> None:
        self.high55 = self.c.rolling(55, min_periods=55).max()
        self.atr20 = atr(self.h, self.l, self.c, 20)
        self.mom126 = self.c / self.c.shift(126) - 1

    def candidates(self, i: int) -> list[str]:
        if not self.market_ok.iloc[i]:
            return []
        c = self.c.iloc[i]
        ok = (c >= self.high55.iloc[i]) & (c > self.sma200.iloc[i])
        picks = self.mom126.iloc[i][ok].reindex(self.tradable).dropna()
        return list(picks.sort_values(ascending=False).index)

    def initial_stop(self, ticker: str, i: int, price: float) -> float | None:
        a = self.atr20[ticker].iloc[i - 1]
        return price - 2 * a if pd.notna(a) else price * 0.9

    def update_stop(self, pos: dict, i: int) -> float | None:
        t = pos["ticker"]
        a = self.atr20[t].iloc[i]
        if pd.isna(a):
            return pos["stop"]
        return max(pos["stop"], pos["high_close"] - 3 * a)


class MomentumSwing(Strategy):
    """Each Friday hold the strongest stocks of the last 6 months; drop the ones that fade.

    Buy the top 5 by 6-month return (skipping the last week to avoid chasing a
    spike) among stocks above their 50-day average. Sell when a stock falls out
    of the top 15, closes below its 50-day average, or hits a 10% stop loss.
    """
    name = "momentum"
    label = "주간 모멘텀"
    description = ("매주 금요일, 최근 6개월 가장 강한 종목 5개를 보유합니다. 순위가 15위 밖으로 "
                   "밀리거나 50일 평균 아래로 내려가면 팔고, 손절 -10%.")
    stop_pct = 0.10
    keep_rank = 15

    def _prepare(self) -> None:
        self.sma50 = sma(self.c, 50)
        self.score = self.c.shift(5) / self.c.shift(126) - 1
        self.week_end = self._week_ends(self.c.index)

    @staticmethod
    def _week_ends(idx: pd.DatetimeIndex):
        """True on the last trading day of each week."""
        week = pd.Series(idx.to_period("W-FRI"), index=idx)
        out = (week != week.shift(-1)).to_numpy().copy()
        out[-1] = idx[-1].dayofweek == 4
        return out

    def _ranked(self, i: int) -> pd.Series:
        c = self.c.iloc[i]
        ok = c > self.sma50.iloc[i]
        return self.score.iloc[i][ok].reindex(self.tradable).dropna().sort_values(ascending=False)

    def candidates(self, i: int) -> list[str]:
        if not self.week_end[i] or not self.market_ok.iloc[i]:
            return []
        return list(self._ranked(i).index)

    def initial_stop(self, ticker: str, i: int, price: float) -> float:
        return price * (1 - self.stop_pct)

    def exit_reason(self, pos: dict, i: int) -> str | None:
        t = pos["ticker"]
        if self.c[t].iloc[i] < self.sma50[t].iloc[i]:
            return "50일 평균 이탈"
        if self.week_end[i] and t not in self._ranked(i).index[: self.keep_rank]:
            return "모멘텀 순위 하락"
        return None


class MomentumBearExit(MomentumSwing):
    """Weekly momentum that also sells everything when the market turns down.

    The plain version only stops buying in a bear market and keeps what it
    holds, which is where its -37% drawdown came from.
    """
    name = "momentum_bear"
    label = "주간 모멘텀 + 하락장 매도"
    description = ("주간 모멘텀과 같지만, S&P500 종가가 200일 평균 아래로 내려가면 "
                   "보유 종목을 모두 팔고 현금으로 기다립니다.")

    def exit_reason(self, pos: dict, i: int) -> str | None:
        if not self.market_ok.iloc[i]:
            return "하락장 전량 매도"
        return super().exit_reason(pos, i)


class MomentumBearExitVol(MomentumBearExit):
    """Adds volatility sizing: jumpy stocks get a smaller slice.

    A stock whose typical daily move (ATR) is 2% of its price gets the full
    20%; one that moves 4% a day gets 10%. Unused money stays in cash.
    """
    name = "momentum_bear_vol"
    label = "주간 모멘텀 + 하락장 매도 + 변동성 비중"
    description = ("위 전략에 더해, 하루 변동폭이 큰 종목은 적게 삽니다 "
                   "(하루 평균 2% 움직이면 20%, 4% 움직이면 10%).")
    risk_per_atr = 0.004

    def _prepare(self) -> None:
        super()._prepare()
        self.atr_pct = atr(self.h, self.l, self.c, 20) / self.c

    def weight(self, ticker: str, i: int) -> float:
        a = self.atr_pct[ticker].iloc[i]
        cap = 1 / self.max_positions
        return cap if pd.isna(a) or a <= 0 else min(cap, self.risk_per_atr / a)


class EarningsGap(Strategy):
    """Follow a stock that jumps on big news (usually earnings) and keeps its gain.

    Post-earnings drift: after a strong surprise, prices tend to keep moving
    the same way for weeks because investors react slowly. Without earnings
    data we spot the event from price and volume: the stock opens at least 4%
    above yesterday's close on at least 3x its usual volume and closes above
    its open. Buy the next open, stop at the gap day's low, sell when the close
    drops below the 20-day average or after 60 trading days.
    """
    name = "earnings_gap"
    label = "실적 갭 상승 추종"
    description = ("평소 3배 넘는 거래량으로 4% 이상 갭 상승하고 그날 시가 위에서 끝난 종목"
                   "(대개 실적 발표)을 다음 날 삽니다. 손절은 갭 당일 저가, 20일 평균을 "
                   "깨거나 60거래일이 지나면 팝니다.")
    gap = 0.04
    vol_mult = 3.0
    max_days = 60

    def _prepare(self) -> None:
        if self.v is None:
            raise ValueError("실적 갭 전략에는 거래량 데이터가 필요합니다.")
        self.sma20 = sma(self.c, 20)
        avg_vol = self.v.rolling(50, min_periods=50).mean().shift(1)
        gap = self.o / self.c.shift(1) - 1
        self.signal = ((gap >= self.gap) & (self.v >= self.vol_mult * avg_vol)
                       & (self.c > self.o))
        self.gap_size = gap

    def candidates(self, i: int) -> list[str]:
        if not self.market_ok.iloc[i]:
            return []
        hit = self.signal.iloc[i].reindex(self.tradable).fillna(False).astype(bool)
        return list(self.gap_size.iloc[i][hit[hit].index].sort_values(ascending=False).index)

    def initial_stop(self, ticker: str, i: int, price: float) -> float:
        low = self.l[ticker].iloc[i - 1]
        return low if pd.notna(low) and low < price else price * 0.92

    def exit_reason(self, pos: dict, i: int) -> str | None:
        t = pos["ticker"]
        if i - pos["entry_i"] >= 3 and self.c[t].iloc[i] < self.sma20[t].iloc[i]:
            return "20일 평균 이탈"
        if i - pos["entry_i"] + 1 >= self.max_days:
            return "보유기간 만료"
        return None


class HighProximity(Strategy):
    """Each Friday hold the stocks closest to their 52-week high.

    George & Hwang (2004): investors anchor on the 52-week high and hesitate to
    buy near it, so stocks close to it tend to keep rising. Hold the 5 stocks
    with the highest close / 52-week-high ratio (above their 200-day average),
    ties broken by 6-month return; sell when the ratio drops below 0.85 or at a
    10% stop loss. (Many stocks sit exactly at their high, so selling on a
    rank drop just churned the ties and paid costs for nothing.)
    """
    name = "high52"
    label = "52주 신고가 근접"
    description = ("매주 금요일, 52주 최고가에 가장 가까운 종목을 빈자리만큼 삽니다(동률이면 6개월 "
                   "수익률 순). 최고가 대비 15% 넘게 밀리면 팔고, 손절 -10%.")
    stop_pct = 0.10
    floor = 0.85

    def _prepare(self) -> None:
        self.ratio = self.c / self.c.rolling(252, min_periods=252).max()
        self.mom126 = self.c / self.c.shift(126) - 1
        self.week_end = MomentumSwing._week_ends(self.c.index)

    def _ranked(self, i: int) -> pd.Series:
        ok = self.c.iloc[i] > self.sma200.iloc[i]
        frame = pd.DataFrame({"ratio": self.ratio.iloc[i].round(3), "mom": self.mom126.iloc[i]})
        frame = frame[ok].reindex(self.tradable).dropna()
        return frame.sort_values(["ratio", "mom"], ascending=False)["ratio"]

    def candidates(self, i: int) -> list[str]:
        if not self.week_end[i] or not self.market_ok.iloc[i]:
            return []
        return list(self._ranked(i).index)

    def initial_stop(self, ticker: str, i: int, price: float) -> float:
        return price * (1 - self.stop_pct)

    def exit_reason(self, pos: dict, i: int) -> str | None:
        t = pos["ticker"]
        if self.ratio[t].iloc[i] < self.floor:
            return "최고가 대비 15% 하락"
        return None


# Candidates in the current report. Pullback and breakout lost to buying the
# S&P 500 in the first round (PR #1) and are kept only for reference.
ALL = [MomentumSwing, MomentumBearExitVol, EarningsGap, HighProximity]
