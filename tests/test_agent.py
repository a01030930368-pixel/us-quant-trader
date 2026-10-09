from datetime import datetime

from scripts.mintory_agent import KST, due_now, next_run, session_day


def k(*a):
    return datetime(*a, tzinfo=KST)


def test_session_day_rolls_back_after_midnight():
    assert session_day(k(2026, 10, 13, 2, 0)) == "2026-10-12"
    assert session_day(k(2026, 10, 12, 23, 50)) == "2026-10-12"


def test_due_window_and_weekdays():
    assert not due_now(k(2026, 10, 12, 23, 44), None)          # Mon before 23:45
    assert due_now(k(2026, 10, 12, 23, 45), None)              # Mon 23:45
    assert due_now(k(2026, 10, 13, 3, 0), "2026-10-09")        # PC on at 3am: catch up Mon
    assert not due_now(k(2026, 10, 13, 3, 0), "2026-10-12")    # already ran
    assert not due_now(k(2026, 10, 13, 6, 0), None)            # market closed
    assert not due_now(k(2026, 10, 10, 23, 50), None)          # Saturday


def test_next_run():
    assert next_run(k(2026, 10, 12, 9, 0), None) == k(2026, 10, 12, 23, 45)
    assert next_run(k(2026, 10, 12, 23, 50), "2026-10-12") == k(2026, 10, 13, 23, 45)
    assert next_run(k(2026, 10, 9, 23, 50), "2026-10-09") == k(2026, 10, 12, 23, 45)  # Fri -> Mon
    now = k(2026, 10, 13, 1, 0)
    assert next_run(now, None) == now
