"""미국 증시(NYSE) 휴장일과 조기 폐장일. 규칙으로 계산해서 인터넷이 필요 없다.

휴장: 새해, 마틴 루터 킹 데이, 대통령의 날, 성금요일, 메모리얼 데이, 준틴스(2022~),
독립기념일, 노동절, 추수감사절, 크리스마스. 주말에 걸리면 토요일은 금요일, 일요일은 월요일에 쉰다
(단, 새해가 토요일이면 전년 12/31은 쉬지 않는다).
조기 폐장(뉴욕 13:00): 독립기념일 전날, 추수감사절 다음 날, 크리스마스 이브 (평일일 때).
특별 휴장(대통령 장례 등)은 미리 알 수 없어 넣지 않았다.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import pandas as pd


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),                 # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                 # Presidents' Day
        _easter(year) - timedelta(days=2),           # Good Friday
        _last_weekday(year, 5, 0),                   # Memorial Day
        _observed(date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                 # Labor Day
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving
        _observed(date(year, 12, 25)),               # Christmas
    }
    ny = date(year, 1, 1)
    if ny.weekday() != 5:                            # a Saturday New Year is not observed
        days.add(_observed(ny))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))       # Juneteenth
    return frozenset(days)


def is_holiday(d: date) -> bool:
    return d in holidays(d.year)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def next_trading_day(d: date) -> date:
    """The first trading day after d."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def early_closes(year: int) -> frozenset[date]:
    out = set()
    for d in (date(year, 7, 3), _nth_weekday(year, 11, 3, 4) + timedelta(days=1), date(year, 12, 24)):
        if is_trading_day(d):
            out.add(d)
    return frozenset(out)


def holiday_list(start: int, end: int) -> list[pd.Timestamp]:
    return sorted(pd.Timestamp(d) for y in range(start, end + 1) for d in holidays(y))


@lru_cache(maxsize=1)
def bday() -> pd.offsets.CustomBusinessDay:
    """One US trading day, for pandas date arithmetic."""
    return pd.offsets.CustomBusinessDay(holidays=holiday_list(2000, 2045))


@lru_cache(maxsize=1)
def bmonth_end() -> pd.offsets.CustomBusinessMonthEnd:
    """The last US trading day of a month."""
    return pd.offsets.CustomBusinessMonthEnd(holidays=holiday_list(2000, 2045))


def upcoming(start: date, days: int = 400) -> dict:
    """Holidays and early closes from start on, for the dashboard clock."""
    end = start + timedelta(days=days)
    years = range(start.year, end.year + 1)
    pick = lambda s: sorted(d.isoformat() for y in years for d in s(y) if start <= d <= end)
    return {"holidays": pick(holidays), "early_closes": pick(early_closes)}
