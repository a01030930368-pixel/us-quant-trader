import json
from datetime import date
from typing import ClassVar

from quant import readiness, telegram_bot

ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "MAX_ORDER_USD": "50,000",
       "MAX_DAILY_LOSS_PCT": "10", "KIWOOM_LIVE_APP_KEY": "LIVEKEY123", "KIWOOM_LIVE_SECRET_KEY": "LIVESECRET456"}


def write(paper, days=25, model_drift=0.0, trades=()):
    paper.mkdir(parents=True, exist_ok=True)
    dates = [f"2026-11-{d:02d}" for d in range(1, days + 1)]
    eq = [{"date": d, "equity": 100_000 + i * 100} for i, d in enumerate(dates)]
    series = {r["date"]: r["equity"] * (1 + model_drift * i / days) for i, r in enumerate(eq)}
    (paper / "equity.jsonl").write_text("\n".join(json.dumps(r) for r in eq), encoding="utf-8")
    (paper / "trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades), encoding="utf-8")
    (paper / "state.json").write_text(json.dumps({"backtest": {"series": series}}), encoding="utf-8")


AGENT = {"last_run": "2026-11-25T23:46:00+09:00", "last_result": "ok", "missed": []}


def by_id(items):
    return {i["id"]: i for i in items}


def test_all_ready(tmp_path):
    write(tmp_path)
    (tmp_path / readiness.KEY_CHECK).write_text(json.dumps({"ok": True, "time": "2026-11-26T09:00", "cash_usd": 1234.5}))
    items = readiness.checklist(tmp_path, ENV, AGENT, date(2026, 11, 26), wake=True)
    assert {i["id"]: i["status"] for i in items} == dict.fromkeys(
        ["days", "gap", "runs", "orders", "telegram", "limit", "loss", "keys", "api", "wake", "paused"], "ok")
    assert readiness.summary(items)["ready"]
    assert "$1,234.50" in by_id(items)["api"]["detail"]


def test_problems_are_flagged(tmp_path):
    write(tmp_path, days=5, model_drift=0.2,
          trades=[{"date": "2026-11-20", "status": "rejected"}, {"date": "2026-11-01", "status": "pending"}])
    (tmp_path / "STOP").write_text("")
    agent = {"last_run": "2026-11-25T23:46", "last_result": "error", "missed": ["2026-11-24"]}
    items = by_id(readiness.checklist(tmp_path, {"MAX_DAILY_LOSS_PCT": "0"}, agent, date(2026, 11, 26)))
    assert items["days"]["status"] == "warn"
    assert items["gap"]["status"] == "warn"
    assert items["runs"]["status"] == "warn" and "2026-11-24" in items["runs"]["detail"]
    assert items["orders"]["status"] == "warn" and "1건" in items["orders"]["detail"]
    assert items["telegram"]["status"] == items["limit"]["status"] == items["keys"]["status"] == "fail"
    assert items["loss"]["status"] == "warn"
    assert items["api"]["status"] == "todo"
    assert items["paused"]["status"] == "warn"
    assert "wake" not in items                       # not on Windows: cannot tell
    assert not readiness.summary(list(items.values()))["ready"]


def test_empty_paper_folder(tmp_path):
    items = by_id(readiness.checklist(tmp_path / "none", {}, {}, date(2026, 11, 26)))
    assert items["gap"]["status"] == items["runs"]["status"] == "todo"
    assert items["loss"]["status"] == "ok"           # default 10% when not set


class FakeBroker:
    seen: ClassVar[list] = []

    def __init__(self, key, secret, mock=True):
        FakeBroker.seen.append(mock)
        self.key = key

    def cash_usd(self):
        if self.key == "BAD":
            raise RuntimeError("token failed for appkey BAD secret LIVESECRET456")
        return 987.654


def test_check_live_keys_reads_real_server_only(tmp_path):
    rec = readiness.check_live_keys(ENV, tmp_path, FakeBroker)
    assert rec["ok"] and rec["cash_usd"] == 987.65 and FakeBroker.seen[-1] is False
    assert json.loads((tmp_path / readiness.KEY_CHECK).read_text())["ok"]


def test_check_live_keys_masks_keys_in_errors(tmp_path):
    env = ENV | {"KIWOOM_LIVE_APP_KEY": "BAD"}
    rec = readiness.check_live_keys(env, tmp_path, FakeBroker)
    saved = (tmp_path / readiness.KEY_CHECK).read_text()
    assert not rec["ok"] and "LIVESECRET456" not in saved and "***" in saved
    assert not readiness.check_live_keys({}, tmp_path, FakeBroker)["ok"]


def test_telegram_check_command(tmp_path):
    write(tmp_path)
    items = readiness.checklist(tmp_path, ENV, AGENT, date(2026, 11, 26))
    report = {"items": items, "summary": readiness.summary(items)}
    act = telegram_bot.Actions(state=dict, agent=dict, paused=lambda: False, set_paused=lambda v: None,
                               refresh=lambda: "ok", readiness=lambda: report)
    text = telegram_bot.handle("/점검", act)
    assert text.startswith("🧾 실계좌 전환 점검표") and "⬜ 실계좌 연결 확인" in text and "점검만" in text
    assert "/check" in telegram_bot.help_text()
