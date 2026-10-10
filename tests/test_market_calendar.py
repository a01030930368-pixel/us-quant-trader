from datetime import date

import pandas as pd

from quant import market_calendar as mc

# NYSE published calendars
NYSE_2026 = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
             "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]
NYSE_2027 = ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
             "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"]


def test_holidays_match_nyse():
    assert sorted(d.isoformat() for d in mc.holidays(2026)) == NYSE_2026
    assert sorted(d.isoformat() for d in mc.holidays(2027)) == NYSE_2027
    assert date(2021, 12, 31) not in mc.holidays(2021)      # Saturday New Year is not observed
    assert date(2021, 6, 18) not in mc.holidays(2021)       # Juneteenth starts in 2022


def test_early_closes():
    assert sorted(d.isoformat() for d in mc.early_closes(2026)) == ["2026-11-27", "2026-12-24"]
    assert date(2025, 7, 3) in mc.early_closes(2025)


def test_trading_days():
    assert not mc.is_trading_day(date(2026, 11, 26)) and mc.is_trading_day(date(2026, 11, 27))
    assert mc.next_trading_day(date(2026, 4, 2)) == date(2026, 4, 6)      # Good Friday + weekend
    assert pd.Timestamp("2026-04-02") + mc.bday() == pd.Timestamp("2026-04-06")


def test_upcoming_for_dashboard():
    up = mc.upcoming(date(2026, 10, 10), days=90)
    assert up == {"holidays": ["2026-11-26", "2026-12-25", "2027-01-01"],
                  "early_closes": ["2026-11-27", "2026-12-24"]}


def test_next_rebalance_skips_good_friday():
    from quant.trader.runner import TraderConfig, next_rebalance
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    # 3/31 month end + 5 trading days skips Good Friday 4/3 -> 4/8, order 4/9
    assert next_rebalance(pd.Timestamp("2026-04-02"), cfg) == {"signal_date": "2026-04-08",
                                                               "order_date": "2026-04-09",
                                                               "tranche": 2, "tranches": 4}
