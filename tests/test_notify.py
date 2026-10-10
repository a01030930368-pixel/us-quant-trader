import json

from quant import notify
from scripts.notify_setup import set_env

EQ = [{"date": "2026-10-08", "equity": 100000.0}, {"date": "2026-10-09", "equity": 100500.0}]
STATE = {"initial_capital": 100000, "alerts": []}


def test_trade_message_lists_orders_and_summary():
    trades = [{"side": "buy", "symbol": "QQQ", "qty": 65, "price": 750.19, "status": "filled"},
              {"side": "buy", "symbol": "TQQQ", "qty": 3, "price": 81.0, "status": "rejected",
               "message": "예수금 부족"}]
    text = notify.run_message("auto", "ok", STATE, trades, EQ)
    assert "🔴 매수 QQQ 65주 @ $750.19\n    ✅ 체결" in text
    assert "거부 (예수금 부족)" in text
    assert "💵 평가금액 $100,500.00\n📈 오늘 +0.50%\n📊 누적 +0.50%" in text
    assert "\n\n🛒 주문\n" in text


def test_quiet_day_respects_daily_switch():
    assert notify.run_message("auto", "ok", STATE, [], EQ, daily=False) is None
    assert "주문 없이 기록만" in notify.run_message("auto", "ok", STATE, [], EQ, daily=True)


def test_problems_and_pause_always_notify():
    st = {"alerts": [{"level": "error", "message": "계좌 조회에 실패해 매매하지 않았습니다"}]}
    text = notify.run_message("auto", "error (exit 1)", st, [], [], daily=False)
    assert "실행에 실패" in text and "계좌 조회에 실패" in text
    st = {"alerts": [{"level": "error", "message": "STOP 파일이 있어 매매를 멈췄습니다."}]}
    text = notify.run_message("auto", "ok", st, [], [], daily=False)
    assert "일시정지 중" in text and "STOP" not in text


def test_send_needs_setup_and_never_raises(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    assert not notify.send("x", env)
    env.write_text("TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    sent = []

    class R:
        ok = True

    def fake_post(url, json, timeout):
        sent.append((url, json))
        return R()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send("hello", env)
    assert sent == [("https://api.telegram.org/bot1:abc/sendMessage", {"chat_id": "42", "text": "hello"})]

    def boom(*a, **k):
        raise notify.requests.ConnectionError("offline")

    monkeypatch.setattr(notify.requests, "post", boom)
    assert notify.send("hello", env) is False


def test_notify_run_only_reports_new_trades(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "state.json").write_text(json.dumps(STATE), encoding="utf-8")
    old = {"side": "sell", "symbol": "IEF", "qty": 1, "status": "filled"}
    new = {"side": "buy", "symbol": "UPRO", "qty": 2, "status": "pending"}
    (paper / "trades.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    texts = []
    monkeypatch.setattr(notify, "send", lambda text, path: texts.append(text) or True)
    assert notify.notify_run("auto", "ok", paper, trades_before=1, path=env)
    assert "UPRO" in texts[0] and "IEF" not in texts[0] and "체결 확인 중" in texts[0]


def test_set_env_keeps_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KIWOOM_APP_KEY=k\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    set_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "2"}, env)
    assert env.read_text(encoding="utf-8").splitlines() == [
        "KIWOOM_APP_KEY=k", "TELEGRAM_CHAT_ID=2", "TELEGRAM_BOT_TOKEN=t"]


def test_weekly_message():
    eq = [{"date": "2026-10-09", "equity": 100000.0, "benchmark_close": 600.0},
          {"date": "2026-10-12", "equity": 100300.0, "benchmark_close": 603.0},
          {"date": "2026-10-16", "equity": 101000.0, "benchmark_close": 606.0}]
    st = {"initial_capital": 100000, "benchmark": "SPY", "next_rebalance": {"order_date": "2026-10-22"},
          "backtest": {"series": {"2026-10-09": 100000.0, "2026-10-16": 101500.0}}}
    trades = [{"date": "2026-10-15", "side": "buy", "symbol": "QQQ", "qty": 3, "price": 750.0,
               "status": "filled", "fee": 5.6},
              {"date": "2026-10-08", "side": "buy", "symbol": "TQQQ", "qty": 1, "price": 80.0,
               "status": "filled", "fee": 1.0}]
    text = notify.weekly_message("2026-10-16", st, eq, trades)
    assert "10/12(월) ~ 10/16(금)" in text
    assert "📈 이번 주 +1.00% (+$1,000.00)" in text
    assert "· SPY +1.00% · 차이 +0.00%p" in text
    assert "· 백테스트 +1.50% · 차이 -0.50%p" in text
    assert "매매 1건 · 수수료 $5.60" in text and "TQQQ" not in text
    assert "리밸런싱 주문일 10/22(목)" in text
    assert notify.weekly_message("2026-10-23", st, eq, trades) is None


def test_weekly_message_first_week_uses_initial_capital():
    eq = [{"date": "2026-10-12", "equity": 99000.0}]
    text = notify.weekly_message("2026-10-16", {"initial_capital": 100000}, eq, [])
    assert "📉 이번 주 -1.00% (-$1,000.00)" in text and "매매 없음" in text


def test_preview_message():
    from quant.notify import preview_message
    pv = {"order_date": "2026-10-15", "as_of": "2026-10-13",
          "orders": [{"symbol": "UPRO", "side": "sell", "qty": 10, "value": 1000.0},
                     {"symbol": "IEF", "side": "buy", "qty": 20, "value": 1900.5}]}
    text = preview_message(pv)
    assert text.startswith("📣 내일 밤 리밸런싱 예정") and "10/15(목) 23:45" in text
    assert "🔵 매도 UPRO 10주 (약 $1,000.00)" in text and "🔴 매수 IEF 20주 (약 $1,900.50)" in text
    assert "10/13(화) 장 마감 가격 기준" in text and "일시정지" not in text
    assert "⏸" in preview_message(pv, paused=True)
    assert preview_message({**pv, "orders": []}) is None


def test_rehearsal_message():
    state = {"cash": 1000.0, "alerts": [], "positions": [{"qty": 2, "last_price": 50.0}],
             "planned_orders": [{"side": "buy", "symbol": "QQQ", "qty": 3, "value": 1500.0}]}
    text = notify.rehearsal_message("ok", state)
    assert "리허설" in text and "🔴 매수 QQQ 3주" in text and "$1,000.00" in text and "$100.00" in text
    assert "주문은 하나도 나가지 않았어요" in text
    assert notify.rehearsal_message("ok", {"cash": 1.0, "alerts": []}) is None          # quiet night
    failed = notify.rehearsal_message("error (exit 1)", state)
    assert "실패" in failed and "QQQ" not in failed                                     # stale plan hidden


def test_failed_run_says_why_and_does_not_call_old_numbers_today():
    eq = [{"date": "2026-10-08", "equity": 100.0}, {"date": "2026-10-09", "equity": 101.0}]
    text = notify.run_message("auto", "error (exit 1)", {}, [], eq, reason="키움 서버 점검 중")
    assert "키움 서버 점검 중" in text and "오늘" not in text and "10/9(금) 기록 기준" in text
    assert "agent.log" not in text
