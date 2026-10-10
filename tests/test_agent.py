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


# -- dashboard server and buttons ----------------------------------------------
import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

import scripts.mintory_agent as agent  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    paper = tmp_path / "paper"
    monkeypatch.setattr(agent, "PORT", port)
    monkeypatch.setattr(agent, "PAPER", paper)
    monkeypatch.setattr(agent, "STOP", paper / "STOP")
    monkeypatch.setattr(agent, "STATE", paper / "agent.json")
    monkeypatch.setattr(agent, "LOG", paper / "logs" / "agent.log")
    release = threading.Event()
    calls = []

    def fake_run(args=agent.JOBS["run"]):
        calls.append(args)
        release.wait(5)
        return 0

    monkeypatch.setattr(agent, "run_trader", fake_run)
    srv = agent.start_server()
    yield f"http://127.0.0.1:{port}", paper, release, calls
    release.set()
    srv.shutdown()
    srv.server_close()


def call(url, body=None, headers=None):
    hdrs = {"X-Mintory": "1"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers=hdrs, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_serves_dashboard_with_controls(server):
    url, *_ = server
    code, html = call(url + "/")
    assert code == 200 and "btn-run" in html and '"control"' in html


def test_rejects_posts_from_other_pages(server):
    url, paper, *_ = server
    assert call(url + "/api/pause", {"paused": True}, {"X-Mintory": ""})[0] == 403
    assert call(url + "/api/pause", {"paused": True}, {"Host": "evil.example:80"})[0] == 403
    assert call(url + "/", headers={"Host": "evil.example"})[0] == 403
    assert not (paper / "STOP").exists()


def test_pause_toggles_stop_file(server):
    url, paper, *_ = server
    code, body = call(url + "/api/pause", {"paused": True})
    assert code == 200 and json.loads(body)["paused"] and (paper / "STOP").exists()
    call(url + "/api/pause", {"paused": False})
    assert not (paper / "STOP").exists()


def test_one_job_at_a_time(server):
    url, paper, release, calls = server
    code, body = call(url + "/api/refresh", {})
    assert code == 200 and json.loads(body)["busy"] == "refresh"
    assert call(url + "/api/run", {})[0] == 409
    release.set()
    for _ in range(50):
        status = json.loads(call(url + "/api/status")[1])
        if not status["busy"]:
            break
        time.sleep(0.05)
    assert status["last_job"]["name"] == "refresh" and status["last_job"]["result"] == "ok"
    assert calls == [agent.JOBS["refresh"]] and "--check" in calls[0]


def test_agent_alive_and_single_instance(server):
    assert agent.agent_alive()
    assert agent.start_server() is None


def test_port_taken_spots_an_old_agent_that_binds_without_serving(server):
    held = socket.socket()
    try:
        held.bind(("127.0.0.1", 0))
        agent.PORT = held.getsockname()[1]   # monkeypatched back by the fixture
        assert agent.port_taken() and not agent.agent_alive()
    finally:
        held.close()


def test_quotes_endpoint_cleans_symbols(server, monkeypatch):
    from quant import live
    seen = []
    monkeypatch.setattr(live, "quotes", lambda symbols: seen.append(symbols) or {"quotes": {}, "as_of": None})
    url, *_ = server
    code, body = call(url + "/api/quotes?symbols=qqq,UPRO,%3Cx%3E")
    assert code == 200 and json.loads(body)["quotes"] == {}
    assert seen == [["QQQ", "UPRO"]]
    assert call(url + "/api/quotes?symbols=QQQ", headers={"Host": "evil.example"})[0] == 403


# -- missed evenings -----------------------------------------------------------


def test_missed_days():
    assert agent.missed_days(k(2026, 10, 13, 10, 0), "2026-10-12") == []
    assert agent.missed_days(k(2026, 10, 13, 3, 0), "2026-10-09") == []        # Mon still in window
    assert agent.missed_days(k(2026, 10, 13, 10, 0), "2026-10-09") == ["2026-10-12"]
    assert agent.missed_days(k(2026, 10, 15, 10, 0), "2026-10-12") == ["2026-10-13", "2026-10-14"]
    assert agent.missed_days(k(2026, 10, 12, 10, 0), "2026-10-09") == []       # weekend
    assert agent.missed_days(k(2026, 10, 13, 10, 0), None) == []


def test_stay_awake_window():
    assert not agent.should_stay_awake(k(2026, 10, 12, 23, 0), "2026-10-09")
    assert agent.should_stay_awake(k(2026, 10, 12, 23, 30), "2026-10-09")
    assert agent.should_stay_awake(k(2026, 10, 13, 1, 0), "2026-10-09")        # catching up
    assert not agent.should_stay_awake(k(2026, 10, 12, 23, 50), "2026-10-12")  # done


def test_check_missed_alerts_once(tmp_path, monkeypatch):
    from quant import notify
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    agent.write_state({"last_day": "2026-10-09"})
    now = k(2026, 10, 13, 10, 0)
    assert agent.check_missed(now, agent.read_state()) == ["2026-10-12"]
    assert agent.check_missed(now, agent.read_state()) == ["2026-10-12"]
    assert len(sent) == 1 and "10/12(월)" in sent[0] and "10/13 23:45" in sent[0]
    assert agent.read_state()["missed"] == ["2026-10-12"]
    agent.check_missed(k(2026, 10, 14, 10, 0), agent.read_state())
    assert len(sent) == 2 and "10/13(화)" in sent[1] and "10/12" not in sent[1]
    agent.update_state(last_day="2026-10-14")
    assert agent.check_missed(k(2026, 10, 15, 10, 0), agent.read_state()) == []
    assert agent.read_state()["missed"] == []


def test_wake_task_xml_is_valid():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    xml = agent.wake_task_xml(Path(r"C:\Py & Co\pythonw.exe"), Path("/home/me/mintory/scripts/mintory_agent.py"))
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Settings/t:WakeToRun", ns).text == "true"
    assert root.find("t:Actions/t:Exec/t:Command", ns).text == r"C:\Py & Co\pythonw.exe"
    assert "T23:40:00+09:00" in root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns).text


def test_weekly_due():
    assert agent.weekly_due(k(2026, 10, 17, 6, 59), None) is None            # Sat before 7
    assert agent.weekly_due(k(2026, 10, 17, 7, 0), None) == "2026-10-16"
    assert agent.weekly_due(k(2026, 10, 18, 22, 0), None) == "2026-10-16"    # Sunday catch-up
    assert agent.weekly_due(k(2026, 10, 18, 22, 0), "2026-10-16") is None    # already sent
    assert agent.weekly_due(k(2026, 10, 19, 9, 0), None) is None             # Monday


def test_send_weekly_refreshes_then_sends_once(tmp_path, monkeypatch):
    from quant import notify
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    monkeypatch.setattr(agent, "PAPER", tmp_path)
    order = []
    monkeypatch.setattr(agent, "run_trader", lambda args: order.append(args) or 0)
    monkeypatch.setattr(notify, "configured", lambda *a: True)
    monkeypatch.setattr(notify, "settings", lambda *a: {"weekly": True, "daily": True})
    monkeypatch.setattr(notify, "notify_weekly", lambda week, paper: order.append(week) or True)
    monkeypatch.setattr(notify, "notify_run", lambda *a, **kw: False)
    agent.send_weekly("2026-10-16")
    assert order == [agent.JOBS["refresh"], "2026-10-16"]
    assert agent.read_state()["weekly_sent"] == "2026-10-16"


def test_backup_due():
    assert agent.backup_due(k(2026, 10, 13, 6, 59), None) is None
    assert agent.backup_due(k(2026, 10, 13, 7, 0), "2026-10-12") == "2026-10-13"
    assert agent.backup_due(k(2026, 10, 17, 9, 0), None) == "2026-10-17"     # weekends too
    assert agent.backup_due(k(2026, 10, 13, 23, 0), "2026-10-13") is None


def test_make_backup_records_state(tmp_path, monkeypatch):
    from quant import notify
    monkeypatch.setattr(agent, "STATE", tmp_path / "paper" / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    monkeypatch.setattr(agent, "PAPER", tmp_path / "paper")
    monkeypatch.setattr(agent, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(notify, "read_env", lambda *a: {})
    agent.write_state({"last_day": "2026-10-12"})
    agent.make_backup("2026-10-13")
    assert (tmp_path / "backups" / "paper-2026-10-13.zip").exists()
    assert agent.read_state()["backup_day"] == "2026-10-13"


def test_preview_due():
    pv = {"order_date": "2026-10-15"}
    assert agent.preview_due("2026-10-14", pv, None) == "2026-10-15"
    assert agent.preview_due("2026-10-14", pv, "2026-10-15") is None          # sent already
    assert agent.preview_due("2026-10-13", pv, None) is None                  # two days ahead
    assert agent.preview_due("2026-10-16", {"order_date": "2026-10-19"}, None) == "2026-10-19"  # Fri -> Mon
    assert agent.preview_due("2026-10-14", None, None) is None


def test_send_preview_once(tmp_path, monkeypatch):
    from quant import notify
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    monkeypatch.setattr(agent, "PAPER", tmp_path)
    (tmp_path / "state.json").write_text(json.dumps({"preview": {"order_date": "2026-10-15"}}))
    sent = []
    monkeypatch.setattr(notify, "notify_preview", lambda paper: sent.append(paper) or True)
    agent.send_preview("2026-10-14")
    agent.send_preview("2026-10-14")
    assert sent == [tmp_path] and agent.read_state()["preview_sent"] == "2026-10-15"


def test_close_refresh_due_after_each_us_session():
    assert agent.close_refresh_due(k(2026, 10, 13, 6, 59), None) is None              # Tue before 7
    assert agent.close_refresh_due(k(2026, 10, 13, 7, 0), None) == "2026-10-13"       # after Mon session
    assert agent.close_refresh_due(k(2026, 10, 13, 9, 0), "2026-10-13") is None       # done today
    assert agent.close_refresh_due(k(2026, 10, 17, 8, 0), None) == "2026-10-17"       # Sat: Fri session
    assert agent.close_refresh_due(k(2026, 10, 18, 8, 0), None) is None               # Sun
    assert agent.close_refresh_due(k(2026, 10, 19, 8, 0), None) is None               # Mon: no Sun session
    assert agent.close_refresh_due(k(2026, 10, 13, 23, 50), None) is None             # tonight's run instead


def test_failure_reason_picks_the_error_message():
    out = ("Traceback (most recent call last):\n  File \"x.py\", line 1\n"
           "RuntimeError: .env 에 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 가 없습니다.\n")
    assert agent.failure_reason(out) == ".env 에 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 가 없습니다."
    assert agent.failure_reason("requests.exceptions.ConnectionError: 연결 실패\n") == "연결 실패"
    assert agent.failure_reason("뭔가 잘못됨\n\n") == "뭔가 잘못됨"
    assert agent.failure_reason("") == ""


def test_failed_job_keeps_its_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    monkeypatch.setattr(agent, "PAPER", tmp_path)

    def boom(args):
        with open(agent.LOG, "a", encoding="utf-8") as f:
            f.write("RuntimeError: 키움 서버에 연결하지 못했습니다\n")
        return 1
    monkeypatch.setattr(agent, "run_trader", boom)
    assert agent._run_job("refresh") == "error (exit 1)"
    assert agent.read_state()["last_job"]["reason"] == "키움 서버에 연결하지 못했습니다"


def test_no_auto_run_on_us_holidays():
    assert not agent.is_trading_evening("2026-11-26")                          # Thanksgiving
    assert not agent.due_now(k(2026, 11, 26, 23, 50), "2026-11-25")
    assert agent.next_run(k(2026, 11, 26, 9, 0), "2026-11-25") == k(2026, 11, 27, 23, 45)
    assert agent.missed_days(k(2026, 11, 28, 10, 0), "2026-11-25") == ["2026-11-27"]
    assert agent.preview_due("2026-11-25", {"order_date": "2026-11-27"}, None) == "2026-11-27"


def test_retry_reason():
    acct = [{"level": "error", "message": "계좌 조회에 실패해 매매하지 않았습니다: timeout"}]
    assert agent.retry_reason("ok", [], 0) is None
    assert agent.retry_reason("error (exit 1)", [], 0) == "error (exit 1)"
    assert agent.retry_reason("ok", acct, 0).startswith("계좌 조회에 실패")
    assert agent.retry_reason("error (exit 1)", acct, 2) is None              # orders went out
    assert agent.retry_reason("ok", [{"level": "error", "message": "STOP 파일이 있어"}], 0) is None


def test_run_auto_retries_then_reports_once(tmp_path, monkeypatch):
    from quant import notify
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    monkeypatch.setattr(agent, "PAPER", tmp_path)
    codes = [1, 1, 0]
    monkeypatch.setattr(agent, "run_trader", lambda args: codes.pop(0))
    sent, reports = [], []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(notify, "notify_run", lambda job, result, paper, before, **kw: reports.append(result) or True)
    monkeypatch.setattr(agent, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: k(2026, 10, 12, 23, 46)),
                                                         "fromisoformat": staticmethod(datetime.fromisoformat)}))
    agent.run_auto(k(2026, 10, 12, 23, 45))
    r = agent.read_state()["retry"]
    assert r["attempt"] == 1 and r["at"].startswith("2026-10-12T23:56") and len(sent) == 1 and not reports
    assert agent.retry_due(k(2026, 10, 12, 23, 50), r) is None
    assert agent.retry_due(k(2026, 10, 12, 23, 57), r) == 1
    agent.run_auto(k(2026, 10, 12, 23, 57), 1)                  # fails again: quiet
    assert agent.read_state()["retry"]["attempt"] == 2 and len(sent) == 1 and not reports
    agent.run_auto(k(2026, 10, 13, 0, 7), 2)                    # succeeds: one normal report
    st = agent.read_state()
    assert st["retry"] is None and reports == ["ok"] and st["last_result"] == "ok"
    assert st["last_day"] == "2026-10-12"


def test_no_retry_after_the_window():
    late = k(2026, 10, 13, 4, 55)
    assert agent.session_day(late + agent.RETRY_GAP) != agent.session_day(late)
    assert not agent.in_run_window(k(2026, 10, 13, 5, 5))


def test_rehearsal_runs_after_the_nightly_run_and_reports_changes_only(tmp_path, monkeypatch):
    from quant import notify
    monkeypatch.setattr(agent, "STATE", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    monkeypatch.setattr(agent, "PAPER", tmp_path)
    monkeypatch.setattr(agent, "REHEARSAL", tmp_path / "rehearsal")
    jobs, sent = [], []
    plan = [{"side": "buy", "symbol": "QQQ", "qty": 3, "value": 1500.0}]

    def fake_trader(args):
        jobs.append(args)
        if "--rehearsal" in args:
            (tmp_path / "rehearsal").mkdir(exist_ok=True)
            (tmp_path / "rehearsal" / "state.json").write_text(json.dumps(
                {"cash": 1000.0, "alerts": [], "planned_orders": plan}), encoding="utf-8")
        return 0

    monkeypatch.setattr(agent, "run_trader", fake_trader)
    monkeypatch.setattr(notify, "notify_run", lambda *a, **kw: True)
    monkeypatch.setattr(notify, "notify_rehearsal", lambda result, d: sent.append(result) or True)
    env = {"KIWOOM_LIVE_APP_KEY": "k", "KIWOOM_LIVE_SECRET_KEY": "s"}
    monkeypatch.setattr(notify, "read_env", lambda: env)

    agent.run_auto(k(2026, 10, 12, 23, 45))
    assert jobs == [agent.JOBS["run"], agent.JOBS["rehearsal"]] and sent == ["ok"]
    assert agent.rehearse("2026-10-12") is None                     # once per evening
    agent.run_auto(k(2026, 10, 13, 23, 45))                         # same plan: no message
    assert len(jobs) == 4 and sent == ["ok"]
    plan.append({"side": "sell", "symbol": "IEF", "qty": 1, "value": 90.0})
    agent.run_auto(k(2026, 10, 14, 23, 45))                         # plan changed: message
    assert sent == ["ok", "ok"]
    env["REHEARSAL"] = "0"
    agent.run_auto(k(2026, 10, 15, 23, 45))
    assert len(jobs) == 7                                           # switched off
    env.clear()
    assert not agent.rehearsal_on(env)                              # no live keys: never runs
    assert "--rehearsal" in agent.JOBS["rehearsal"] and "--check" in agent.JOBS["rehearsal"]


def test_withdraw_api_and_command(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "PAPER", tmp_path)
    monkeypatch.setattr(agent, "LOG", tmp_path / "agent.log")
    (tmp_path / "state.json").write_text(json.dumps({"cash": 5000.0, "fx": {"usdkrw": 1500.0}, "positions": []}))
    code, out = agent.withdraw_api({"amount": "150만"})
    assert code == 200 and out["withdraw"]["amount_usd"] == 1000.0 and "출금 준비를 요청했어요" in out["message"]
    assert agent.withdraw_api({"amount": "$99999"})[0] == 400
    assert "$1,000.00" in agent.withdraw_command("")
    assert "취소했어요" in agent.withdraw_command("취소")
    assert agent.withdraw_api({"cancel": True}) == (200, {"withdraw": None})
    assert "요청했어요" in agent.withdraw_command("$500")
    (tmp_path / "flows.jsonl").write_text(json.dumps({"time": "2026-10-12T23:50", "amount": 500.0}) + "\n")
    monkeypatch.setattr(agent, "read_state", dict)
    rep = agent.readiness_report()
    assert rep["flows"]["paper"][0]["amount"] == 500.0 and rep["flows"]["live"] == []
