import time

from quant import notify
from quant import telegram_bot as tb

STATE = {"initial_capital": 100000, "cash": 2171.22, "updated_at": "2026-10-10T16:35:00+09:00",
         "positions": [{"symbol": "QQQ", "qty": 65, "avg_price": 750.19, "last_price": 751.24},
                       {"symbol": "TQQQ", "qty": 302, "avg_price": 80.96, "last_price": 81.28}],
         "next_rebalance": {"order_date": "2026-10-15"}}


def actions(**kw):
    box = {"paused": False, "refreshed": 0}

    def refresh():
        box["refreshed"] += 1
        return "ok"
    act = tb.Actions(state=lambda: STATE, agent=lambda: {"next_run": "2026-10-12T23:45"},
                     paused=lambda: box["paused"], set_paused=lambda v: box.update(paused=v),
                     refresh=refresh, **kw)
    return act, box


def test_command_aliases():
    assert tb.command("/balance") == "balance"
    assert tb.command("/balance@MintoryBot extra") == "balance"
    assert tb.command("잔고") == "balance"
    assert tb.command("/일시정지") == "pause"
    assert tb.command("정지") == "pause"
    assert tb.command("/start") == "help"
    assert tb.command("/?") == "help" and tb.command("?") == "help" and tb.command("명령어") == "help"
    assert tb.command("hello") is None
    assert tb.command("") is None


def test_balance_uses_quotes_when_available():
    act, _ = actions(quotes=lambda syms: {"quotes": {"QQQ": {"price": 760.0, "change": 0.01}},
                                          "as_of": "2026-10-12T23:50:00+09:00"})
    text = tb.handle("/balance", act)
    total = 2171.22 + 65 * 760.0 + 302 * 81.28
    assert f"${total:,.2f}" in text
    assert "시세 10/12(월) 23:50" in text and "오늘 +1.00%" in text
    assert text.index("QQQ") < text.index("TQQQ")       # biggest first


def test_balance_without_quotes_uses_record():
    act, _ = actions()
    text = tb.handle("잔고", act)
    assert "기록 10/10(토) 16:35" in text and "현금 · $2,171.22" in text


def test_pause_resume_and_status():
    act, box = actions()
    assert "일시정지했어요" in tb.handle("/pause", act) and box["paused"]
    assert "이미 일시정지" in tb.handle("/pause", act)
    assert "일시정지 중" in tb.handle("/status", act)
    assert "다시 시작" in tb.handle("/resume", act) and not box["paused"]
    assert "다음 자동 실행 · 10/12(월) 23:45" in tb.handle("상태", act)


def test_refresh_busy_and_help():
    act, box = actions()
    assert "현금" in tb.handle("/refresh", act) and box["refreshed"] == 1
    act.refresh = lambda: None
    assert "다른 작업" in tb.handle("/refresh", act)
    assert "/pause" in tb.handle("뭐해", act)


def test_serve_only_answers_owner_and_hides_token(monkeypatch):
    monkeypatch.setattr(notify, "settings", lambda: {"token": "123:SECRET", "chat_id": "42", "daily": True})
    sent, logs, offset = [], [], {}
    now = int(time.time())
    updates = [
        {"update_id": 5, "message": {"chat": {"id": 7}, "date": now, "text": "/pause"}},       # stranger
        {"update_id": 6, "message": {"chat": {"id": 42}, "date": now - 3600, "text": "/pause"}},  # old
        {"update_id": 7, "message": {"chat": {"id": 42}, "date": now, "text": "/status"}},
    ]

    def api(token, method, **params):
        return {"result": updates} if method == "getUpdates" else {"ok": True}
    monkeypatch.setattr(tb, "_api", api)
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    act, box = actions()
    tb.serve(act, lambda: offset.get("o"), lambda n: offset.__setitem__("o", n),
             log=logs.append, once=True)
    assert offset["o"] == 8
    assert len(sent) == 1 and "Mintory 상태" in sent[0] and not box["paused"]

    def boom(token, method, **params):
        raise RuntimeError("404 for url https://api.telegram.org/bot123:SECRET/getUpdates")
    monkeypatch.setattr(tb, "_api", boom)
    tb.serve(act, lambda: None, lambda n: None, log=logs.append, once=True)
    assert logs and "SECRET" not in logs[-1] and "***" in logs[-1]


def test_next_command_shows_preview():
    from quant.telegram_bot import Actions, handle
    pv = {"order_date": "2026-10-15", "as_of": "2026-10-13",
          "orders": [{"symbol": "IEF", "side": "buy", "qty": 3, "value": 290.0}]}
    act = Actions(state=lambda: {"preview": pv}, agent=dict, paused=lambda: False,
                  set_paused=lambda v: None, refresh=lambda: "ok")
    text = handle("/다음", act)
    assert "10/15(목) 23:45" in text and "🔴 매수 IEF 3주 (약 $290.00)" in text and "10/13(화) 장 마감" in text
    assert "바뀌는 종목이 없어요" in handle("/next", Actions(state=lambda: {"preview": {**pv, "orders": []}}, agent=dict,
                                                          paused=lambda: False, set_paused=lambda v: None,
                                                          refresh=lambda: "ok"))
    assert "미리보기가 없어요" in handle("next", Actions(state=dict, agent=dict, paused=lambda: False,
                                                    set_paused=lambda v: None, refresh=lambda: "ok"))
