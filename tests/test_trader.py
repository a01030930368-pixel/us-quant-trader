import json
from datetime import datetime, timedelta, timezone

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
    closes = state["closes"]
    assert closes["date"] == str(close.index[-1].date()) and closes["prev_date"] == str(close.index[-2].date())
    assert closes["prev"]["SPY"] == pytest.approx(close["SPY"].iloc[-2], abs=1e-3)


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


def test_session_cutoff_after_kst_midnight():
    from quant.trader.runner import session_cutoff
    kst = timezone(timedelta(hours=9))
    assert session_cutoff(datetime(2026, 10, 15, 23, 45, tzinfo=kst)) == pd.Timestamp("2026-10-15")
    # 01:00 KST = 12:00 New York, Oct 15 still trading: its bar is not finished
    assert session_cutoff(datetime(2026, 10, 16, 1, 0, tzinfo=kst)) == pd.Timestamp("2026-10-15")
    assert session_cutoff(datetime(2026, 10, 16, 7, 0, tzinfo=kst)) == pd.Timestamp("2026-10-16")
    assert session_cutoff(datetime(2026, 12, 16, 1, 0, tzinfo=kst)) == pd.Timestamp("2026-12-15")


def test_catch_up_after_midnight_ignores_running_session(tmp_path, close):
    px = canary_close(close)
    spiked = px.loc[:"2026-10-08"].copy()
    spiked.iloc[-1] *= 3  # Oct 8 is still trading at 01:00 KST Oct 9
    r = canary_runner(tmp_path)
    s = r.run(close=spiked, now=datetime(2026, 10, 8, 16, tzinfo=timezone.utc), execute=False)
    assert s["signal"]["as_of"] == "2026-10-07"


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
    arch = new.dir / "archive" / "momentum_blend_multi_2026-10-09"
    assert (arch / "trades.jsonl").exists() and (arch / "equity.jsonl").exists()
    assert s["started_at"] == "2026-10-09"  # 00:00 KST Oct 10 = the Oct 9 US session
    assert s["strategy"]["id"] == "momentum_blend_multi_canary_eem_t4"
    fresh = trades(new)
    assert fresh and all(t["note"] == "분할 리밸런싱" for t in fresh)
    assert {t["symbol"] for t in fresh} <= set(held_before) | {t["symbol"] for t in s["targets"]}
    meta = json.loads((new.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["strategy_id"] == "momentum_blend_multi_canary_eem_t4"


def test_fee_is_taken_from_cash_once_the_batch_settles(tmp_path, close):
    r = make_runner(tmp_path)
    t = "2026-10-10T00:18:00+09:00"
    rows = [
        {"time": t, "symbol": "QQQ", "side": "buy", "qty": 10, "price": 750.0, "fee": 0.0, "status": "pending"},
        {"time": t, "symbol": "IEF", "side": "sell", "qty": 50, "price": 100.0, "fee": 0.0, "status": "filled"},
        {"time": t, "symbol": "GLD", "side": "buy", "qty": 5, "price": 300.0, "fee": 0.0, "status": "rejected"},
    ]
    (r.dir / "trades.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    meta = {"fee_batch": {"time": t, "cash_before": 10_000.0}}
    # buy 7,500 - sell 5,000 = 2,500 out; 31.25 more left = 0.25% of 12,500 traded
    r._settle_fees(meta, 7_468.75)
    assert "fee_batch" in meta and all(x["fee"] == 0 for x in trades(r))  # QQQ still pending
    r._reconcile({"QQQ": {"qty": 10, "avg_price": 750.0}})
    r._settle_fees(meta, 7_468.75)
    fees = {x["symbol"]: x["fee"] for x in trades(r)}
    assert fees == {"QQQ": 18.75, "IEF": 12.5, "GLD": 0.0} and "fee_batch" not in meta


def test_fee_ignores_cash_moves_that_are_not_fees(tmp_path, close):
    r = make_runner(tmp_path)
    t = "2026-10-10T00:18:00+09:00"
    (r.dir / "trades.jsonl").write_text(json.dumps(
        {"time": t, "symbol": "QQQ", "side": "buy", "qty": 10, "price": 750.0, "fee": 0.0,
         "status": "filled"}) + "\n", encoding="utf-8")
    meta = {"fee_batch": {"time": t, "cash_before": 10_000.0}}
    r._settle_fees(meta, 3_000.0)  # 500 more than a fee could be
    assert trades(r)[0]["fee"] == 0 and "fee_batch" not in meta


def test_simulated_run_keeps_its_own_fees(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close, execute=True, now=datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    tr = trades(r)
    assert tr and all(x["fee"] > 0 for x in tr)
    assert "fee_batch" not in json.loads((r.dir / "meta.json").read_text(encoding="utf-8"))


# -- live-account safety --------------------------------------------------------

def test_daily_loss_limit_skips_orders(tmp_path, close):
    r = make_runner(tmp_path)
    (r.dir / "equity.jsonl").write_text(json.dumps({"date": "2026-10-07", "equity": 12_000.0}) + "\n",
                                        encoding="utf-8")
    state = r.run(close, execute=True, now=datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    assert "하루 손실 한도" in errors(state) and not trades(r)
    r.cfg.max_daily_loss = None
    r.run(close, execute=True, now=datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    assert trades(r)


def test_live_mode_needs_order_limit_and_announces(tmp_path, close):
    sent = []
    cfg = TraderConfig(state_dir=tmp_path / "paper")
    broker = SimBroker(cfg.state_dir / "sim_account.json", starting_cash=10_000.0)
    r = Runner(broker, cfg, mode="kiwoom_live", notify=sent.append)
    state = r.run(close, execute=True, now=datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    assert "MAX_ORDER_USD" in errors(state) and not trades(r) and not sent
    r.cfg.max_order_usd = 9_000.0
    r.run(close, execute=True, now=datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    assert trades(r) and len(sent) == 1 and "실계좌 주문" in sent[0]


def test_mock_mode_never_announces(tmp_path, close):
    sent = []
    cfg = TraderConfig(state_dir=tmp_path / "paper")
    r = Runner(SimBroker(cfg.state_dir / "sim_account.json", starting_cash=10_000.0), cfg,
               mode="kiwoom_mock", notify=sent.append)
    r.run(close, execute=True, now=datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc))
    assert trades(r) and not sent


def test_safety_limits_from_env():
    from scripts.run_trader import safety_limits
    assert safety_limits({}) == {}
    assert safety_limits({"MAX_ORDER_USD": "$50,000", "MAX_DAILY_LOSS_PCT": "8%"}) == {
        "max_order_usd": 50_000.0, "max_daily_loss": 0.08}
    assert safety_limits({"MAX_DAILY_LOSS_PCT": "0"}) == {"max_daily_loss": None}
    assert safety_limits({"MAX_ORDER_USD": "lots"}) == {}


# -- backtest comparison -------------------------------------------------------

def test_model_weights_match_runner_targets(close):
    from quant.trader.runner import model_weights, staggered_targets
    px = canary_close(close)
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    today = pd.Timestamp("2026-10-08")              # a run during the 10/8 session
    rows, sig = staggered_targets(px, today, cfg)
    got = {}
    for t in rows:
        got[t["symbol"]] = got.get(t["symbol"], 0.0) + t["weight"] * (1 - cfg.cash_buffer)
    w = model_weights(px.loc[:today], cfg).loc[sig]
    assert got == pytest.approx({k: v for k, v in w.items() if v > 1e-9})


def test_shadow_backtest_follows_model_returns(close):
    from quant.trader.runner import model_weights, shadow_backtest
    cfg = TraderConfig()
    rows = [{"date": "2026-10-01", "equity": 10_000.0}, {"date": "2026-10-02", "equity": 10_050.0},
            {"date": "2026-10-05", "equity": 9_990.0}]
    bt = shadow_backtest(close, cfg, rows, cost_bps=0)
    assert bt["from"] == "2026-10-01" and list(bt["series"]) == ["2026-10-01", "2026-10-02", "2026-10-05"]
    w = model_weights(close, cfg)
    r = close.pct_change()
    want = 10_000.0
    for d in ["2026-10-02", "2026-10-05"]:
        prev = close.index[close.index.get_loc(pd.Timestamp(d)) - 1]
        want *= 1 + (w.loc[prev] * r.loc[d]).sum()
    assert bt["series"]["2026-10-05"] == pytest.approx(want, abs=0.01)
    assert shadow_backtest(close, cfg, [], 10) is None


def test_runner_writes_backtest_and_keeps_it_when_paused(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close=close.loc[:"2026-10-06"], now=datetime(2026, 10, 6, 15, tzinfo=timezone.utc))
    st = r.run(close=close.loc[:"2026-10-07"], now=datetime(2026, 10, 7, 15, tzinfo=timezone.utc))
    assert st["backtest"]["cost_bps"] == 10.0
    assert set(st["backtest"]["series"]) == {"2026-10-06", "2026-10-07"}
    (r.dir / "STOP").write_text("x")
    paused = r.run(close=close.loc[:"2026-10-08"], now=datetime(2026, 10, 8, 15, tzinfo=timezone.utc))
    assert set(paused["backtest"]["series"]) == {"2026-10-06", "2026-10-07", "2026-10-08"}
    # early exits (stale prices) still keep the last comparison
    stale = r.run(close=close.loc[:"2026-09-30"], now=datetime(2026, 10, 9, 15, tzinfo=timezone.utc))
    assert stale["backtest"] == paused["backtest"]


def test_paused_run_records_the_day_without_orders(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close=close.loc[:"2026-10-06"], now=datetime(2026, 10, 6, 15, tzinfo=timezone.utc))
    n = len(trades(r))
    (r.dir / "STOP").write_text("x")
    s = r.run(close=close.loc[:"2026-10-07"], now=datetime(2026, 10, 7, 15, tzinfo=timezone.utc))
    assert "STOP" in errors(s) and len(trades(r)) == n
    assert not any("점검 모드" in a["message"] for a in s["alerts"])
    rows = (r.dir / "equity.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["date"] for x in rows] == ["2026-10-06", "2026-10-07"]


# -- next rebalance preview ----------------------------------------------------

def test_preview_matches_what_the_order_day_would_trade(close):
    from quant.trader.runner import next_rebalance, preview_rebalance, staggered_targets
    px = canary_close(close)
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    today = pd.Timestamp("2026-10-09")
    pv = preview_rebalance(px, today, cfg, {}, 10_000.0, {})
    assert pv["order_date"] == next_rebalance(today, cfg)["order_date"] == "2026-10-15"
    assert pv["as_of"] == "2026-10-08"
    # the same prices carried to the order day give the same targets
    done = px[px.index < today]
    days = pd.bdate_range(done.index[-1] + pd.offsets.BDay(1), pv["order_date"])
    flat = pd.concat([done, pd.DataFrame([done.iloc[-1].values] * len(days), index=days, columns=done.columns)])
    rows, _ = staggered_targets(flat, pd.Timestamp(pv["order_date"]), cfg)
    want = {}
    for t in rows:
        want[t["symbol"]] = want.get(t["symbol"], 0.0) + t["weight"]
    assert pv["weights"] == pytest.approx(want, abs=1e-4)
    assert {o["symbol"] for o in pv["orders"]} == set(pv["weights"])
    assert all(o["side"] == "buy" for o in pv["orders"])
    # holding exactly the target means nothing to trade
    price = {t: float(done[t].iloc[-1]) for t in done.columns}
    hold = {t: {"qty": int(10_000 * 0.98 * w // price[t]), "avg_price": price[t]} for t, w in want.items()}
    cash = 10_000 - sum(h["qty"] * price[t] for t, h in hold.items())
    assert preview_rebalance(px, today, cfg, hold, cash, price)["orders"] == []


def test_preview_on_order_day_shows_the_next_one(close):
    from quant.trader.runner import next_rebalance, preview_rebalance
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    px = canary_close(close)
    day = pd.Timestamp(next_rebalance(pd.Timestamp("2026-09-28"), cfg)["order_date"])
    pv = preview_rebalance(px.loc[:day], day, cfg, {}, 10_000.0, {})
    assert pv["order_date"] > str(day.date())


def test_runner_writes_preview(tmp_path, close):
    r = make_runner(tmp_path)
    st = r.run(close=close, now=datetime(2026, 10, 8, 15, tzinfo=timezone.utc))
    assert st["preview"]["order_date"] and "orders" in st["preview"]
    (r.dir / "STOP").write_text("x")
    assert r.run(close=close, now=datetime(2026, 10, 8, 16, tzinfo=timezone.utc))["preview"] == st["preview"]


def test_kiwoom_gets_a_new_token_when_refused(monkeypatch):
    from quant.trader import brokers
    b = brokers.KiwoomBroker("k", "s")
    tokens = iter(["T1", "T2"])
    monkeypatch.setattr(b, "token", lambda: b._token or setattr(b, "_token", next(tokens)) or b._token)
    seen = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._body, self.content = code, body, b"x"
            self.ok = code == 200

        def json(self):
            return self._body

    replies = [R(401, {"return_code": 3, "return_msg": "토큰이 유효하지 않습니다"}), R(200, {"return_code": 0, "ok": 1})]

    def post(url, headers, json, timeout):
        seen.append(headers["authorization"])
        return replies.pop(0)

    monkeypatch.setattr(brokers.requests, "post", post)
    monkeypatch.setattr(brokers.time, "sleep", lambda s: None)
    assert b.call("/x", "id", {})["ok"] == 1
    assert seen == ["Bearer T1", "Bearer T2"]


def test_kiwoom_explains_user_side_errors_without_retrying(monkeypatch):
    from quant.trader import brokers
    b = brokers.KiwoomBroker("k", "s")
    monkeypatch.setattr(b, "token", lambda: "T")
    calls = []

    class R:
        status_code, ok, content = 200, True, b"x"

        def json(self):
            return {"return_code": 8050, "return_msg": "지정단말기 인증에 실패했습니다 (token)"}

    monkeypatch.setattr(brokers.requests, "post", lambda *a, **k: calls.append(1) or R())
    monkeypatch.setattr(brokers.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="IP가 등록"):
        b.call("/x", "ust21160", {})
    assert len(calls) == 1                     # a new token would not help


# -- live-account rehearsal (never orders) --------------------------------------
def test_read_only_broker_refuses_orders(tmp_path):
    from quant.trader.brokers import ReadOnlyBroker
    b = ReadOnlyBroker(SimBroker(tmp_path / "a.json", starting_cash=5_000.0))
    assert b.cash_usd() == 5_000.0 and b.positions() == {}
    with pytest.raises(RuntimeError):
        b.place_order("SPY", "buy", 1, 500.0)


def test_rehearsal_records_planned_orders_without_ordering(tmp_path, close):
    from quant.trader.brokers import ReadOnlyBroker
    cfg = TraderConfig(state_dir=tmp_path / "rehearsal", max_order_usd=50_000)
    inner = SimBroker(tmp_path / "a.json", starting_cash=10_000.0)
    r = Runner(ReadOnlyBroker(inner), cfg, mode="kiwoom_live")
    s = r.run(close=close, now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc), execute=False)
    assert s["planned_orders"] and all(o["side"] == "buy" for o in s["planned_orders"])
    assert trades(r) == [] and inner.positions() == {} and inner.cash_usd() == 10_000.0
    saved = json.loads((cfg.state_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["planned_orders"] == s["planned_orders"] and saved["mode"] == "kiwoom_live"


def test_rehearsal_shows_the_live_order_limit_block(tmp_path, close):
    from quant.trader.brokers import ReadOnlyBroker
    cfg = TraderConfig(state_dir=tmp_path / "rehearsal")
    r = Runner(ReadOnlyBroker(SimBroker(tmp_path / "a.json", starting_cash=10_000.0)), cfg, mode="kiwoom_live")
    s = r.run(close=close, now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc), execute=False)
    assert "MAX_ORDER_USD" in errors(s) and "planned_orders" not in s


def test_run_trader_rehearse_uses_live_keys_read_only(monkeypatch, tmp_path, close):
    from quant.trader import brokers, runner
    from scripts import run_trader

    made = []

    class FakeKiwoom(SimBroker):
        def __init__(self, key, secret, mock=True, exchanges=None):
            made.append((key, secret, mock))
            super().__init__(tmp_path / "live.json", starting_cash=20_000.0)

        def place_order(self, *a, **kw):  # pragma: no cover - must never be reached
            raise AssertionError("rehearsal placed an order")

    monkeypatch.setattr(run_trader, "KiwoomBroker", FakeKiwoom)
    monkeypatch.setattr(runner, "load_prices", lambda *a, **kw: close)
    monkeypatch.setattr(runner, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 10, 8, 14, tzinfo=timezone.utc))}))
    monkeypatch.delenv("KIWOOM_LIVE_APP_KEY", raising=False)
    cfg = TraderConfig(state_dir=tmp_path / "paper", max_order_usd=50_000)
    with pytest.raises(SystemExit):
        run_trader.rehearse(cfg)
    monkeypatch.setenv("KIWOOM_LIVE_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_LIVE_SECRET_KEY", "s")
    run_trader.rehearse(cfg)
    assert made == [("k", "s", False)]
    state = json.loads((tmp_path / "paper" / "rehearsal" / "state.json").read_text(encoding="utf-8"))
    assert state["planned_orders"] and not (tmp_path / "paper" / "state.json").exists()
    assert brokers.ReadOnlyBroker is run_trader.ReadOnlyBroker


def test_exchange_rate_is_recorded_for_won_view(tmp_path, close):
    r = make_runner(tmp_path)
    r.fx_source = lambda: 1388.5
    s = r.run(close=close, now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc))
    assert s["fx"]["usdkrw"] == 1388.5
    assert all(x["fx"] == 1388.5 for x in trades(r))
    eq = [json.loads(x) for x in (r.dir / "equity.jsonl").read_text(encoding="utf-8").splitlines()]
    assert eq[-1]["usdkrw"] == 1388.5
    r.fx_source = lambda: None                       # rate unavailable: keep the last one
    assert r.run(close=close, now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc))["fx"]["usdkrw"] == 1388.5


def test_tranche_detail_adds_up_to_the_targets(close):
    from quant.trader.runner import next_rebalance, staggered_targets, tranche_detail
    cfg = TraderConfig(canary=("EEM",), tranches=4)
    px = canary_close(close)
    today = pd.Timestamp("2026-10-09")
    parts = tranche_detail(px, today, cfg)
    assert [p["n"] for p in parts] == [1, 2, 3, 4]
    assert all(p["decided"] <= "2026-10-08" and isinstance(p["risk_on"], bool) for p in parts)
    total = {}
    for p in parts:
        assert sum(p["weights"].values()) == pytest.approx(1.0, abs=1e-3)
        for s, w in p["weights"].items():
            total[s] = total.get(s, 0.0) + w / 4
    rows, _ = staggered_targets(px, today, cfg)
    want = {}
    for t in rows:
        want[t["symbol"]] = want.get(t["symbol"], 0.0) + t["weight"]
    assert total == pytest.approx(want, abs=1e-3)
    nxt = next_rebalance(today, cfg)
    assert nxt["tranches"] == 4 and 1 <= nxt["tranche"] <= 4


# -- deposits, withdrawals, idle cash ---------------------------------------------
def _flows(r):
    p = r.dir / "flows.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_deposit_is_recorded_invested_and_not_counted_as_gain(tmp_path, close):
    from quant import performance
    sent = []
    r = make_runner(tmp_path)
    r.notify = sent.append
    now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
    r.run(close=close, now=now)
    before = len(trades(r))
    r.broker.acct["cash"] += 5_000.0          # the user deposits (or exchanges won into) $5,000
    r.broker._save()
    s = r.run(close=close, now=now)
    assert [f["amount"] for f in _flows(r)] == [5000.0] and _flows(r)[0]["kind"] == "deposit"
    assert s["principal"] == pytest.approx(s["initial_capital"] + 5000) and s["flows"]["deposits"] == 5000
    assert any("입금" in t for t in sent)
    new = trades(r)[before:]
    assert new and all(t["side"] == "buy" and t["note"] == "남는 현금 투자" for t in new)
    assert s["cash"] < 0.3 * 5000          # most of the deposit went in (whole shares up to ~$1,100)
    rows = [json.loads(x) for x in (r.dir / "equity.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["flow"] == 5000.0
    assert abs(performance.total_return(rows, s["initial_capital"])) < 0.01   # fees only, not +50%
    r.run(close=close, now=now)                # nothing new: no second deposit, no more buys
    assert len(_flows(r)) == 1 and len(trades(r)) == before + len(new)


def test_withdrawal_request_sets_cash_aside_then_finishes(tmp_path, close):
    from quant import performance, withdraw
    sent = []
    r = make_runner(tmp_path, cash=20_000.0)
    r.notify = sent.append
    now = datetime(2026, 10, 8, 14, tzinfo=timezone.utc)
    r.run(close=close, now=now)
    withdraw.request(r.dir, "$6000")
    s = r.run(close=close, now=now)
    sells = [t for t in trades(r) if t["note"] == "출금 준비"]
    assert sells and all(t["side"] == "sell" for t in sells)
    assert s["cash"] >= 6000 and s["withdraw"]["ready"] and any("출금 준비 완료" in t for t in sent)
    assert r.run(close=close, now=now)["withdraw"]["ready"]            # the reserve is not reinvested
    assert not [t for t in trades(r) if t["note"] == "남는 현금 투자"]
    r.broker.acct["cash"] -= 6000.0           # the user withdraws in the Kiwoom app
    r.broker._save()
    s = r.run(close=close, now=now)
    assert _flows(r)[-1]["amount"] == -6000.0 and not (r.dir / "withdraw.json").exists()
    assert "withdraw" not in s and any("요청하신 출금이 끝났어요" in t for t in sent)
    assert s["principal"] == pytest.approx(s["initial_capital"] - 6000)
    rows = [json.loads(x) for x in (r.dir / "equity.jsonl").read_text(encoding="utf-8").splitlines()]
    assert abs(performance.total_return(rows, s["initial_capital"])) < 0.01   # not -30%


def test_withdrawal_is_not_a_daily_loss(tmp_path):
    r = make_runner(tmp_path)
    (r.dir / "equity.jsonl").write_text(json.dumps({"date": "2026-10-07", "equity": 10_000}) + "\n")
    assert "손실 한도" in r._guard([], 7_000, "2026-10-08")
    assert r._guard([], 7_000, "2026-10-08", flow=-3_000) is None


def test_no_flow_while_orders_are_unfilled(tmp_path, close):
    r = make_runner(tmp_path)
    meta = {"seen": {"cash": 1000.0, "qty": {}}}
    (r.dir / "trades.jsonl").write_text(json.dumps({"symbol": "SPY", "status": "pending"}) + "\n")
    assert r._detect_flow(meta, {}, 5000.0, {}) == 0.0
    (r.dir / "trades.jsonl").write_text(json.dumps({"symbol": "SPY", "status": "filled"}) + "\n")
    assert r._detect_flow(meta, {}, 5000.0, {}) == 4000.0
    # shares bought since then explain the cash that left: no flow
    assert r._detect_flow(meta, {"SPY": {"qty": 2, "avg_price": 400.0}}, 200.0, {"SPY": 400.0}) == 0.0
    assert r._detect_flow(meta, {}, 1050.0, {}) == 0.0                  # a small dividend


class SilentOnce(SimBroker):
    """The first order of each symbol in `drop` gets no reply; `lost` decides
    whether it was really dropped (True) or quietly executed (False)."""

    def __init__(self, *a, drop=(), lost=True, silent=1, **kw):
        super().__init__(*a, **kw)
        self.drop, self.lost, self.silent, self.sent = dict.fromkeys(drop, silent), lost, silent, []

    def place_order(self, ticker, side, qty, ref_price):
        self.sent.append(ticker)
        if self.drop.get(ticker):
            self.drop[ticker] -= 1
            if not self.lost:
                super().place_order(ticker, side, qty, ref_price)
            return {"ok": False, "status": "pending", "msg": "키움 응답 없음 - 접수 여부 불확실"}
        return super().place_order(ticker, side, qty, ref_price)


OPEN = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)      # Wed 11:00 ET
CLOSED = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)     # Wed 20:00 ET


def silent_runner(tmp_path, **kw):
    cfg = TraderConfig(state_dir=tmp_path / "paper")
    r = Runner(SilentOnce(cfg.state_dir / "sim_account.json", **kw), cfg)
    r.waits = []
    r.sleep = r.waits.append
    return r


def first_buy(tmp_path, close):
    probe = make_runner(tmp_path / "probe")
    probe.run(close=close, now=OPEN)
    return next(t["symbol"] for t in trades(probe) if t["side"] == "buy")


def test_unanswered_order_is_sent_again_while_market_open(tmp_path, close):
    sym = first_buy(tmp_path, close)
    r = silent_runner(tmp_path, drop=[sym])
    r.run(close=close, now=OPEN)
    assert r.broker.sent.count(sym) == 2 and r.waits == [60.0]
    rec = next(t for t in trades(r) if t["symbol"] == sym)
    assert rec["status"] == "filled" and rec["retried"] == 1
    assert json.loads((r.dir / "meta.json").read_text()).get("last_signal")  # the rebalance counts as done
    assert not [a for a in r.run(close=close, now=OPEN)["alerts"] if "확인하지 못했" in a["message"]]


def test_unanswered_order_that_went_through_is_not_doubled(tmp_path, close):
    sym = first_buy(tmp_path, close)
    r = silent_runner(tmp_path, drop=[sym], lost=False)
    r.run(close=close, now=OPEN)
    assert r.broker.sent.count(sym) == 1
    rec = next(t for t in trades(r) if t["symbol"] == sym)
    assert rec["status"] == "filled" and not rec.get("retried")


def test_unanswered_order_waits_when_market_closed_or_retries_run_out(tmp_path, close):
    sym = first_buy(tmp_path, close)
    r = silent_runner(tmp_path / "closed", drop=[sym])
    state = r.run(close=close, now=CLOSED)
    assert r.broker.sent.count(sym) == 1 and r.waits == []
    assert next(t for t in trades(r) if t["symbol"] == sym)["status"] == "pending"
    assert any("확인하지 못했" in a["message"] for a in state["alerts"])

    r = silent_runner(tmp_path / "busy", drop=[sym], silent=5)
    r.run(close=close, now=OPEN)
    assert r.broker.sent.count(sym) == 3 and len(r.waits) == 2
    rec = next(t for t in trades(r) if t["symbol"] == sym)
    assert rec["status"] == "pending" and rec["retried"] == 2


def test_market_open_window():
    assert Runner._market_open(OPEN) and not Runner._market_open(CLOSED)
    assert not Runner._market_open(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc))  # Saturday
    assert not Runner._market_open(datetime(2026, 10, 7, 19, 58, tzinfo=timezone.utc))  # 2 min to close


def test_lost_order_is_replaced_not_settled_by_the_next_fill(tmp_path, close):
    sym = first_buy(tmp_path, close)
    r = silent_runner(tmp_path, drop=[sym])
    r.run(close=close, now=CLOSED)                       # no reply, market closed: stays pending
    r.run(close=close, now=CLOSED)                       # not done yet: the next run buys it again
    mine = [t for t in trades(r) if t["symbol"] == sym]
    assert [t["status"] for t in mine] == ["cancelled", "filled"]


def test_negative_cash_from_broker_is_not_a_withdrawal(tmp_path, close):
    r = make_runner(tmp_path)
    r.run(close=close, now=CLOSED)
    good = json.loads((r.dir / "state.json").read_text())
    rows = (r.dir / "equity.jsonl").read_text()
    real = r.broker.cash_usd
    r.broker.cash_usd = lambda: real() - 100_000            # Kiwoom mock, 2026-10-13 morning
    r.broker.cash_detail = {"d0": 2166.38, "d2": -95662.41}
    state = r.run(close=close, now=CLOSED)
    assert "예수금" in errors(state) and "d2" in errors(state)
    assert not (r.dir / "flows.jsonl").exists()
    assert (r.dir / "equity.jsonl").read_text() == rows
    assert state["cash"] == good["cash"]
