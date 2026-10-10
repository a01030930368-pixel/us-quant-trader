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
