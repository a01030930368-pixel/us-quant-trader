import json

import pytest

from quant import notify, performance, telegram_bot, withdraw


def test_growth_leaves_out_deposits_and_withdrawals():
    rows = [{"date": "d1", "equity": 110.0}, {"date": "d2", "equity": 220.0, "flow": 100.0},
            {"date": "d3", "equity": 121.0, "flow": -110.0}]
    g = performance.growth(rows, 100.0)
    assert g == pytest.approx([1.1, 1.1 * 120 / 110, 1.1 * 120 / 110 * 231 / 220])
    assert performance.principal(100.0, rows) == 90.0
    assert performance.total_return(rows, 100.0, equity_now=242.0) == pytest.approx(g[-1] * 2 - 1)
    model = {"d1": 110.0, "d2": 215.0, "d3": 100.0}
    mg = performance.model_growth(model, rows, 100.0)
    assert mg["d1"] == pytest.approx(1.1) and mg["d2"] == pytest.approx(1.1 * 115 / 110)


def test_user_story_take_out_part_of_the_profit():
    # 5M won grew to 10M, 3M taken out, the rest keeps going: return stays +100%
    rows = [{"date": "a", "equity": 10_000.0}, {"date": "b", "equity": 7_000.0, "flow": -3_000.0}]
    assert performance.total_return(rows, 5_000.0) == pytest.approx(1.0)
    assert performance.principal(5_000.0, rows) == 2_000.0


@pytest.mark.parametrize("text,usd,krw", [
    ("300만", 2000.0, 3_000_000), ("3,000,000원", 2000.0, 3_000_000), ("300만원", 2000.0, 3_000_000),
    ("$2000", 2000.0, None), ("2000달러", 2000.0, None), ("2000 USD", 2000.0, None), ("1500", 1500.0, None),
    ("450000", 300.0, 450_000), ("1억2천만", 80_000.0, 120_000_000),
])
def test_parse_amount(text, usd, krw):
    assert withdraw.parse_amount(text, 1500.0) == (usd, krw)


def test_parse_amount_errors():
    for bad in ("", "0", "abc"):
        with pytest.raises(ValueError):
            withdraw.parse_amount(bad, 1500.0)
    with pytest.raises(ValueError, match="환율"):
        withdraw.parse_amount("300만", None)


def _paper(tmp_path, cash=1000.0):
    (tmp_path / "state.json").write_text(json.dumps({
        "cash": cash, "fx": {"usdkrw": 1500.0}, "mode": "kiwoom_mock",
        "positions": [{"symbol": "QQQ", "qty": 10, "avg_price": 400.0, "last_price": 500.0}]}))
    (tmp_path / "trades.jsonl").write_text(json.dumps(
        {"date": "2026-01-02", "time": "t", "symbol": "QQQ", "side": "buy", "qty": 10, "price": 400.0,
         "fee": 0, "status": "filled", "fx": 1500.0}) + "\n")
    return tmp_path


def test_request_cancel_and_tax_hint(tmp_path):
    paper = _paper(tmp_path)
    req = withdraw.request(paper, "300만")
    assert req["amount_usd"] == 2000.0 and req["left_usd"] == 2000.0 and withdraw.current(paper)
    # $1,000 of the $2,000 needs selling ($980 above the 2% buffer is cash): 1/5 of the
    # holding's +1.5M won gain is under the 2.5M deduction -> no extra tax
    assert req["tax_krw"] == 0
    with pytest.raises(ValueError, match="평가금액"):
        withdraw.request(paper, "$9000")
    assert "300만" not in withdraw.request_text(req) and "$2,000.00" in withdraw.request_text(req)
    assert withdraw.cancel(paper) and not withdraw.current(paper) and not withdraw.cancel(paper)


def test_telegram_withdraw_and_balance_principal(tmp_path):
    seen = []
    act = telegram_bot.Actions(state=dict, agent=dict, paused=lambda: False, set_paused=lambda v: None,
                               refresh=lambda: "ok", withdraw=lambda arg: seen.append(arg) or "ok")
    assert telegram_bot.handle("/withdraw 300만", act) == "ok" and telegram_bot.handle("출금 취소", act) == "ok"
    assert telegram_bot.handle("/withdraw", act) == "ok" and seen == ["300만", "취소", ""]
    state = {"cash": 7000.0, "positions": [], "initial_capital": 5000.0, "principal": 2000.0}
    rows = [{"date": "a", "equity": 10_000.0}, {"date": "b", "equity": 7_000.0, "flow": -3_000.0}]
    text = telegram_bot.balance_text(state, equity=rows)
    assert "누적 +100.00%" in text and "원금 $2,000.00" in text and "번 돈 +$5,000.00" in text


def test_daily_and_weekly_messages_leave_out_flows():
    rows = [{"date": "2026-10-09", "equity": 10_000.0},
            {"date": "2026-10-12", "equity": 15_100.0, "flow": 5_000.0}]
    text = notify.run_message("auto", "ok", {"initial_capital": 10_000.0}, [], rows)
    assert "오늘 +1.00%" in text and "누적 +1.00%" in text
    week = notify.weekly_message("2026-10-16", {"initial_capital": 10_000.0}, rows, [])
    assert "이번 주 +1.00% (+$100.00)" in week and "입금 $5,000.00" in week
