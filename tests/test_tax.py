import json

import pytest

from quant import tax, telegram_bot


def t(date, side, qty, price, fx=None, fee=0.0, symbol="QQQ", status="filled"):
    row = {"date": date, "time": date + "T23:50", "symbol": symbol, "side": side, "qty": qty,
           "price": price, "fee": fee, "status": status}
    if fx:
        row["fx"] = fx
    return row


def test_fifo_gain_in_won_with_trade_day_rates():
    trades = [t("2026-01-05", "buy", 100, 100.0, fx=1300), t("2026-02-05", "buy", 100, 120.0, fx=1400),
              t("2026-03-05", "sell", 150, 150.0, fx=1350, fee=5.0)]
    r = tax.estimate(trades, [{"symbol": "QQQ", "qty": 50, "last_price": 160.0, "avg_price": 120.0}], 2026, 1380)
    # first 100 @100 x1300, then 50 @120 x1400; proceeds 150 x150 x1350 - fee 5 x1350
    assert r["realized_krw"] == (150 * 150 * 1350 - 5 * 1350 - 100 * 100 * 1300 - 50 * 120 * 1400)
    assert r["realized_usd"] == pytest.approx(150 * 150 - 5 - 100 * 100 - 50 * 120)
    taxable = r["realized_krw"] - 2_500_000
    assert r["taxable_krw"] == taxable and r["tax_krw"] == round(taxable * 0.22)
    assert r["unrealized_krw"] == (50 * 160 * 1380 - 50 * 120 * 1400)
    assert r["deduction_left_krw"] == 0 and not r["estimated_fx"] and r["sells"] == 1


def test_losses_offset_and_deduction():
    trades = [t("2026-01-05", "buy", 10, 100.0, fx=1300), t("2026-02-05", "sell", 10, 101.0, fx=1300),
              t("2026-01-05", "buy", 5, 50.0, fx=1300, symbol="GLD"), t("2026-02-05", "sell", 5, 40.0, fx=1300, symbol="GLD")]
    r = tax.estimate(trades, [], 2026, 1300)
    assert r["realized_krw"] == 10 * 1300 - 50 * 1300 and r["tax_krw"] == 0
    assert r["by_symbol"][0]["symbol"] == "GLD"


def test_other_years_rejected_orders_and_missing_rates():
    trades = [t("2025-06-01", "buy", 10, 100.0), t("2025-12-01", "sell", 5, 130.0),
              t("2026-01-10", "sell", 5, 140.0, status="rejected"), t("2026-01-11", "sell", 5, 150.0)]
    r = tax.estimate(trades, [], 2026, 1000)
    assert r["sells"] == 1 and r["realized_krw"] == 5 * 50 * 1000 and r["estimated_fx"]
    assert tax.estimate(trades, [], 2026, None) is None            # no rate anywhere


def test_holding_without_records_uses_broker_average():
    r = tax.estimate([], [{"symbol": "SPY", "qty": 2, "last_price": 110.0, "avg_price": 100.0}], 2026, 1400)
    assert r["unrealized_krw"] == 2 * 10 * 1400 and r["estimated_fx"]


def test_from_paper_and_telegram(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"fx": {"usdkrw": 1300}, "positions": [], "mode": "kiwoom_mock"}))
    (tmp_path / "trades.jsonl").write_text("\n".join(json.dumps(x) for x in [
        t("2026-01-05", "buy", 100, 100.0, fx=1300), t("2026-02-05", "sell", 100, 150.0, fx=1300)]))
    r = tax.from_paper(tmp_path, 2026)
    assert r["realized_krw"] == 6_500_000 and r["tax_krw"] == 880_000
    text = telegram_bot.tax_text(r, "kiwoom_mock")
    assert "₩880,000" in text and "+₩6,500,000" in text and "모의투자" in text
    assert "환율 기록이 없어" in telegram_bot.tax_text(None)
    assert tax.from_paper(tmp_path / "none") is None
    assert telegram_bot.command("세금") == "tax" and telegram_bot.command("/tax") == "tax"


def test_balance_shows_won():
    text = telegram_bot.balance_text({"cash": 1000.0, "positions": [], "fx": {"usdkrw": 1400.0}})
    assert "약 ₩1,400,000" in text
