import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from quant.trader.brokers import KiwoomBroker, SimBroker
from quant.trader.runner import DATA_TICKERS, Runner, TraderConfig, plan_orders, signal_date


@pytest.fixture
def close():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2015-01-01", "2026-10-08")
    data = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, (len(idx), len(DATA_TICKERS))), axis=0)
    return pd.DataFrame(data, index=idx, columns=DATA_TICKERS)


def make_runner(tmp_path, cash=10_000.0):
    cfg = TraderConfig(state_dir=tmp_path / "paper")
    return Runner(SimBroker(cfg.state_dir / "sim_account.json", starting_cash=cash), cfg)


def trades(r):
    p = r.dir / "trades.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def errors(state):
    return " ".join(a["message"] for a in state["alerts"] if a["level"] == "error")


def test_signal_date_is_last_session_of_previous_month(close):
    assert signal_date(close.index, pd.Timestamp("2026-10-08")) == pd.Timestamp("2026-09-30")


def test_first_run_rebalances_then_waits(tmp_path, close):
    r = make_runner(tmp_path)
    now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
    first = r.run(close=close, now=now)
    done = trades(r)
    assert done and all(t["status"] == "filled" for t in done)
    held = sum(p["qty"] * p["last_price"] for p in first["positions"])
    assert first["cash"] + held == pytest.approx(10_000, rel=0.01)
    assert first["cash"] < 10_000 * 0.15  # whole shares only, prices up to ~$1,100
    assert first["last_rebalance"] == "2026-10-08"
    r.run(close=close, now=now)
    assert len(trades(r)) == len(done)  # already executed this month


def test_check_mode_places_nothing(tmp_path, close):
    r = make_runner(tmp_path)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc), execute=False)
    assert any("점검 모드" in a["message"] for a in s["alerts"])
    assert trades(r) == [] and r.broker.positions() == {}


def test_stop_file_halts(tmp_path, close):
    r = make_runner(tmp_path)
    (r.dir / "STOP").touch()
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert "STOP" in errors(s) and r.broker.positions() == {}


def test_order_cap_blocks(tmp_path, close):
    cfg = TraderConfig(state_dir=tmp_path / "paper", max_order_usd=1_000)
    r = Runner(SimBroker(cfg.state_dir / "sim_account.json", starting_cash=100_000), cfg)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert "한도" in errors(s) and r.broker.positions() == {}


def test_default_cap_allows_large_single_position(tmp_path, close):
    r = make_runner(tmp_path, cash=100_000)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert not errors(s) and r.broker.positions()


def test_plan_sells_before_buys():
    orders = plan_orders({"QQQ": 1.0}, {"QQQ": 100.0, "SPY": 50.0},
                         {"SPY": {"qty": 100, "avg_price": 40.0}}, 0.0, TraderConfig())
    assert [(o["symbol"], o["side"]) for o in orders] == [("SPY", "sell"), ("QQQ", "buy")]


def test_dashboard_files_written(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    state = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 1 and state["mode"] == "simulate"
    assert state["positions"] and state["targets"] and state["signal"]["scores"]
    assert sum(t["weight"] for t in state["targets"]) == pytest.approx(1.0)
    eq = (r.dir / "equity.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(eq) == 1 and json.loads(eq[0])["date"] == "2026-10-08"


def test_stale_prices_halt(tmp_path, close):
    r = make_runner(tmp_path)
    s = r.run(close=close, now=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert "데이터" in errors(s) and r.broker.positions() == {}


class FakeKiwoom(KiwoomBroker):
    def __init__(self, responses):
        super().__init__("k", "s", mock=True, exchanges={"SPY": "NY"})
        self.responses, self.sent = responses, []

    def call(self, path, api_id, body, retries=2):
        self.sent.append((path, api_id, body))
        res = self.responses[api_id]
        if isinstance(res, Exception):
            raise res
        return res


def test_kiwoom_order_bodies():
    k = FakeKiwoom({"ust20000": {"return_code": 0, "ord_no": "000000123"},
                    "ust20001": {"return_code": 0, "ord_no": "000000124"}})
    buy = k.place_order("SPY", "buy", 3, 500.0)
    k.place_order("QQQ", "sell", 2, 400.0)
    assert buy == {"ok": True, "status": "pending", "order_no": "000000123",
                   "fill_price": 500.0, "fee": 0.0, "msg": ""}
    assert k.sent[0] == ("/api/us/ordr", "ust20000",
                         {"stex_tp": "NY", "stk_cd": "SPY", "ord_qty": "3",
                          "ord_uv": "507.50", "trde_tp": "00"})
    sell = k.sent[1][2]
    assert k.sent[1][1] == "ust20001" and sell["stex_tp"] == "ND"
    assert sell["ord_uv"] == "394.00" and sell["trde_tp"] == "00"


def test_kiwoom_order_failure_is_reported():
    k = FakeKiwoom({"ust20000": RuntimeError("ust20000 실패 (HTTP 200): 장운영시간이 아닙니다")})
    res = k.place_order("SPY", "buy", 1, 500.0)
    assert not res["ok"] and "장운영" in res["msg"]


def test_kiwoom_positions_parsing():
    k = FakeKiwoom({"ust21070": {"return_code": 0, "result_list": [
        {"stk_cd": "SPY ", "poss_qty": "000000000005", "frgn_stk_book_uv": "+512.3400"},
        {"stk_cd": "GLD", "poss_qty": "0", "frgn_stk_book_uv": ""}]}})
    assert k.positions() == {"SPY": {"qty": 5, "avg_price": 512.34}}


def test_kiwoom_cash_uses_settled_projection():
    k = FakeKiwoom({"ust21160": {"return_code": 0, "d0_usd_fx_entr": "10000.00",
                                 "d1_usd_fx_entr": "8500.25", "d2_usd_fx_entr": "",
                                 "won_entr": "+00000000500000"}})
    assert k.cash_usd() == 8500.25


def test_targets_match_momentum_blend(close):
    from quant.strategies import momentum_blend
    from quant.trader.runner import sleeve_targets

    cfg = TraderConfig()
    sig = pd.Timestamp("2026-09-30")
    got = {}
    for t in sleeve_targets(close, sig, cfg):
        got[t["symbol"]] = got.get(t["symbol"], 0.0) + t["weight"]
    px = close.rename(columns={"TQQQ": "QQQ3X", "UPRO": "SPY3X"})
    want = momentum_blend(px, aggr_share=0.5, lookbacks=[21, 63, 126, 252]).loc[sig]
    want = {{"QQQ3X": "TQQQ", "SPY3X": "UPRO"}.get(k, k): v for k, v in want.items() if v > 1e-9}
    assert got == pytest.approx(want)
    assert cfg.strategy_id == "momentum_blend_multi"
    assert cfg.strategy_name == "혼합 50/50 멀티기간 모멘텀"


class FailingBroker(SimBroker):
    def positions(self):
        raise RuntimeError("ust21070 실패 (HTTP 200): RC9002 모의투자 서비스가 지연되고 있습니다")


def test_account_read_failure_is_reported_not_raised(tmp_path, close):
    cfg = TraderConfig(state_dir=tmp_path / "paper")
    r = Runner(FailingBroker(cfg.state_dir / "sim_account.json"), cfg)
    s = r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert "계좌 조회" in errors(s) and "RC9002" in errors(s)
    assert (r.dir / "state.json").exists() and trades(r) == []


def test_account_read_failure_keeps_last_holdings(tmp_path, close):
    ok = make_runner(tmp_path)
    cfg = ok.cfg
    first = ok.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert first["positions"]
    r = Runner(FailingBroker(cfg.state_dir / "sim_account.json"), cfg)
    s = r.run(close=close, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert "계좌 조회" in errors(s)
    assert [(p["symbol"], p["qty"]) for p in s["positions"]] == \
        [(p["symbol"], p["qty"]) for p in first["positions"]]
    assert s["cash"] == first["cash"] and s["targets"]


def test_kiwoom_retries_when_server_busy(monkeypatch):
    import quant.trader.brokers as b

    class Resp:
        def __init__(self, body):
            self.body, self.status_code, self.ok, self.content = body, 200, True, b"x"

        def json(self):
            return self.body

    replies = [Resp({"return_code": 20, "return_msg": "[2000](RC9002:모의투자 서비스가 지연되고 있습니다. 잠시후 재시도 바랍니다.)"}),
               Resp({"return_code": 20, "return_msg": "[2000](1:Timeover response)"}),
               Resp({"return_code": 0, "result_list": []})]
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    k = KiwoomBroker("k", "s")
    k._token, k._token_exp = "t", 1e12
    assert k.positions() == {} and not replies


def test_kiwoom_retries_read_timeout_but_not_orders(monkeypatch):
    import quant.trader.brokers as b

    class Resp:
        status_code, ok, content = 200, True, b"x"

        def json(self):
            return {"return_code": 0, "result_list": []}

    calls = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise b.requests.ReadTimeout("Read timed out")
        return Resp()

    monkeypatch.setattr(b.requests, "post", post)
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    k = KiwoomBroker("k", "s")
    k._token, k._token_exp = "t", 1e12
    assert k.positions() == {} and len(calls) == 2

    calls.clear()
    monkeypatch.setattr(b.requests, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(b.requests.ReadTimeout("slow")))
    res = k.place_order("SPY", "buy", 1, 500.0)
    assert not res["ok"] and res["status"] == "pending" and "응답 없음" in res["msg"]


def test_run_trader_refreshes_dashboard(monkeypatch, tmp_path):
    from dashboard import build
    from scripts import run_trader

    seen = []
    monkeypatch.setattr(build, "main", lambda argv: seen.append(argv) or 0)
    run_trader.build_dashboard(tmp_path)
    run_trader.build_dashboard(tmp_path, open_browser=True)
    assert seen == [["--data", str(tmp_path), "--no-open"], ["--data", str(tmp_path)]]


def test_dashboard_reads_runner_output(tmp_path, close):
    from dashboard import build

    r = make_runner(tmp_path)
    r.run(close=close, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    data = build.load(r.dir)
    assert data["state"]["strategy"]["id"] == "momentum_blend_multi"
    assert data["trades"] and data["equity"]
    out = tmp_path / "dash.html"
    assert build.main(["--data", str(r.dir), "--out", str(out), "--no-open"]) == 0
    assert "momentum_blend_multi" in out.read_text(encoding="utf-8")


def test_pending_trades_settle_from_balance(tmp_path, close):
    r = make_runner(tmp_path)
    rows = [
        {"symbol": "QQQ", "side": "buy", "qty": 65, "price": 750.11, "status": "pending"},
        {"symbol": "UPRO", "side": "buy", "qty": 158, "price": 155.0, "status": "pending"},
        {"symbol": "IEF", "side": "sell", "qty": 10, "price": 95.0, "status": "pending",
         "held_before": 10},
        {"symbol": "TQQQ", "side": "buy", "qty": 5, "price": 80.0, "status": "pending",
         "held_before": 302},
        {"symbol": "GLD", "side": "buy", "qty": 5, "price": 300.0, "status": "rejected"},
    ]
    (r.dir / "trades.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    r._reconcile({"QQQ": {"qty": 65, "avg_price": 750.185}, "UPRO": {"qty": 145, "avg_price": 155.24},
                  "TQQQ": {"qty": 302, "avg_price": 80.96}})
    got = {(t["symbol"], t["status"], t["price"]) for t in trades(r)}
    assert got == {("QQQ", "filled", 750.185), ("UPRO", "partial", 155.0),
                   ("IEF", "filled", 95.0), ("GLD", "rejected", 300.0),
                   ("TQQQ", "pending", 80.0)}


# -- EEM canary + 4 tranches ---------------------------------------------------

def canary_close(close, eem_up=True):
    px = close.copy()
    step = 0.001 if eem_up else -0.001
    px["EEM"] = 100 * np.cumprod(np.full(len(px), 1 + step))
    return px


def canary_runner(tmp_path):
    cfg = TraderConfig(state_dir=tmp_path / "paper", canary=("EEM",), tranches=4)
    return Runner(SimBroker(cfg.state_dir / "sim_account.json", starting_cash=10_000.0), cfg)


def test_canary_targets_match_backtest_strategy(close):
    from quant.strategies import tranched
    from quant.trader.runner import staggered_targets

    px = canary_close(close)
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    rows, sig = staggered_targets(px, pd.Timestamp("2026-10-09"), cfg)
    assert sig == pd.Timestamp("2026-10-08")
    got = {}
    for t in rows:
        got[t["symbol"]] = got.get(t["symbol"], 0.0) + t["weight"]
    full = pd.concat([px.loc[:"2026-10-08"], px.loc[["2026-10-08"]].set_axis(
        [pd.Timestamp("2026-10-09")])])
    want = tranched(full, strategy="canary_gate", canary=["EEM"], lookbacks=[21, 63, 126, 252],
                    stable_assets=["SPY", "QQQ", "IWM", "EFA", "GLD"],
                    aggr_assets=["TQQQ", "UPRO", "GLD", "TLT"]).loc[sig]
    assert got == pytest.approx({k: v for k, v in want.items() if v > 1e-9})
    assert cfg.strategy_id == "momentum_blend_multi_canary_eem_t4"
    assert "EEM 카나리아" in cfg.strategy_name


def test_canary_off_goes_to_safe(close):
    from quant.trader.runner import staggered_targets
    rows, _ = staggered_targets(canary_close(close, eem_up=False), pd.Timestamp("2026-10-09"),
                                TraderConfig(canary=("EEM",), tranches=4))
    assert {t["symbol"] for t in rows} == {"IEF"}
    assert sum(t["weight"] for t in rows) == pytest.approx(1.0)


def test_canary_ignores_todays_partial_bar(close):
    from quant.trader.runner import staggered_targets
    px = canary_close(close)
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    base, _ = staggered_targets(px.loc[:"2026-10-07"], pd.Timestamp("2026-10-08"), cfg)
    spiked = px.loc[:"2026-10-08"].copy()
    spiked.iloc[-1] *= 3  # today's half-finished bar must not matter
    got, sig = staggered_targets(spiked, pd.Timestamp("2026-10-08"), cfg)
    assert sig == pd.Timestamp("2026-10-07") and got == base


def test_canary_runner_trades_only_when_target_changes(tmp_path, close):
    px = canary_close(close)
    r = canary_runner(tmp_path)
    s = r.run(close=px.loc[:"2026-10-07"], now=datetime(2026, 10, 8, 15, tzinfo=timezone.utc))
    first = trades(r)
    assert first and s["strategy"]["id"] == "momentum_blend_multi_canary_eem_t4"
    assert s["signal"]["canary"][0]["symbol"] == "EEM" and s["signal"]["canary"][0]["risk_on"]
    assert "분할" in first[0]["note"]
    # next day, no tranche date in between: nothing new to trade
    r.run(close=px.loc[:"2026-10-08"], now=datetime(2026, 10, 9, 15, tzinfo=timezone.utc))
    assert len(trades(r)) == len(first)


def test_switching_strategy_archives_old_records_and_rebalances(tmp_path, close):
    px = canary_close(close)
    old = make_runner(tmp_path)
    old.run(close=px.loc[:"2026-10-07"], now=datetime(2026, 10, 8, 15, tzinfo=timezone.utc))
    held_before = old.broker.positions()
    meta = json.loads((old.dir / "meta.json").read_text(encoding="utf-8"))
    meta.pop("strategy_id")  # records written before strategy_id existed
    (old.dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    new = canary_runner(tmp_path)
    new.run(close=px.loc[:"2026-10-08"], now=datetime(2026, 10, 9, 15, tzinfo=timezone.utc),
            execute=False)  # a check run changes nothing
    assert not (new.dir / "archive").exists()

    s = new.run(close=px.loc[:"2026-10-08"], now=datetime(2026, 10, 9, 15, tzinfo=timezone.utc))
    arch = new.dir / "archive" / "momentum_blend_multi_2026-10-10"
    assert (arch / "trades.jsonl").exists() and (arch / "equity.jsonl").exists()
    assert s["started_at"] == "2026-10-10"
    assert s["strategy"]["id"] == "momentum_blend_multi_canary_eem_t4"
    fresh = trades(new)
    assert fresh and all(t["note"] == "분할 리밸런싱" for t in fresh)
    assert {t["symbol"] for t in fresh} <= set(held_before) | {t["symbol"] for t in s["targets"]}
    meta = json.loads((new.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["strategy_id"] == "momentum_blend_multi_canary_eem_t4"
