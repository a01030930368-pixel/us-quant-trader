"""Kiwoom refusing this PC's IP: noted for Mintory and /status, never a phone alert."""
import json

from quant import ipcheck
from quant.trader.brokers import KiwoomBroker


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok, self.content = body, status, status < 400, b"x"

    def json(self):
        return self.body


def test_a_refused_ip_is_noted_and_cleared_after_a_good_call(tmp_path, monkeypatch):
    import requests
    answers = [Resp({"return_code": 3, "return_msg": "인증에 실패했습니다[8050:IP가 등록되지 않았습니다.]"}),
               Resp({"token": "T", "expires_dt": "20991231235959"}),
               Resp({"return_code": 0, "d0_usd_fx_entr": "12.5"})]
    monkeypatch.setattr(requests, "post", lambda *a, **kw: answers.pop(0))
    monkeypatch.setattr(ipcheck, "public_ip", lambda fetch=None: "49.143.31.200")
    b = KiwoomBroker("k", "s", mock=False, status_dir=tmp_path)
    b.MIN_GAP = 0
    try:
        b.cash_usd()
    except RuntimeError:
        pass
    rec = ipcheck.blocked(tmp_path)["real"]
    assert rec["code"] == "8050" and rec["ip"] == "49.143.31.200"
    msg = ipcheck.message(rec, "real")
    assert "49.143.31.200" in msg and "API 사용신청" in msg
    assert b.cash_usd() == 12.5
    assert ipcheck.blocked(tmp_path) == {}


def test_no_file_writes_while_all_is_well(tmp_path):
    ipcheck.record(tmp_path, "real", False)
    assert not (tmp_path / ipcheck.STATUS).exists()


def test_public_ip_rejects_garbage():
    assert ipcheck.public_ip(lambda: "49.143.31.200\n") == "49.143.31.200"
    assert ipcheck.public_ip(lambda: "<html>") is None


def test_status_command_shows_the_ip_block():
    from quant.telegram_bot import Actions, handle
    rec = {"blocked": True, "code": "8050", "ip": "49.143.31.200"}
    act = Actions(state=dict, agent=dict, paused=lambda: False, set_paused=lambda v: None,
                  refresh=lambda: "ok", ip=lambda: {"real": rec})
    text = handle("/status", act)
    assert "IP 미등록" in text and "49.143.31.200" in text
    assert "IP 미등록" not in handle("/status", Actions(state=dict, agent=dict, paused=lambda: False,
                                                       set_paused=lambda v: None, refresh=lambda: "ok", ip=dict))


def test_dashboard_gets_the_bar(tmp_path):
    from dashboard import build
    (tmp_path / "state.json").write_text((build.SAMPLE_DIR / "state.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert "ip_block" not in build.load(tmp_path)
    (tmp_path / ipcheck.STATUS).write_text(json.dumps({"real": {"blocked": True, "code": "8050", "ip": "1.2.3.4"}}))
    assert "1.2.3.4" in build.load(tmp_path)["ip_block"][0]["message"]
    assert "1.2.3.4" in build.page(tmp_path, quiet=True)
