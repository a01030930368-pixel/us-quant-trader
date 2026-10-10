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
