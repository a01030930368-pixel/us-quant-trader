"""매일 한 번 실행하는 모의투자 실행기.

    python scripts/run_trader.py --mode sim        # 가상 계좌로 연습 (기본)
    python scripts/run_trader.py --mode kiwoom     # 키움 모의투자 서버
    python scripts/run_trader.py --check           # 주문 없이 계획만 확인
    python scripts/run_trader.py --check --open    # 잔고만 새로 읽고 Mintory 대시보드 열기
    python scripts/run_trader.py --strategy multi   # 예전 전략(월 1회 멀티기간 모멘텀)

실행이 끝날 때마다 Mintory 대시보드(reports/dashboard.html)를 새로 만듭니다.
매매를 즉시 멈추려면 paper 폴더에 STOP 이라는 빈 파일을 만드세요.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.trader.brokers import KiwoomBroker, ReadOnlyBroker, SimBroker  # noqa: E402
from quant.trader.runner import Runner, TraderConfig  # noqa: E402
from scripts.kiwoom_mock_check import load_env  # noqa: E402


# Strategy presets selectable with --strategy (TraderConfig fields).
# canary_eem became the default for paper trading on 2026-10-10.
STRATEGIES = {
    "multi": {},
    "canary_eem": {"canary": ("EEM",), "tranches": 4},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sim", "kiwoom"], default="sim")
    ap.add_argument("--check", action="store_true", help="주문 없이 계획만 출력")
    ap.add_argument("--aggr-share", type=float, default=0.5)
    ap.add_argument("--strategy", choices=sorted(STRATEGIES), default="canary_eem",
                    help="canary_eem = EEM 카나리아 + 4분할(기본), multi = 월 1회 멀티기간 모멘텀")
    ap.add_argument("--sim-cash", type=float, default=10_000.0, help="가상 계좌 시작금(USD)")
    ap.add_argument("--open", action="store_true", help="끝나면 Mintory 대시보드를 브라우저로 열기")
    ap.add_argument("--no-dashboard", action="store_true", help="대시보드를 새로 만들지 않음")
    ap.add_argument("--rehearsal", action="store_true",
                    help="실계좌 리허설: 실계좌 잔고만 읽고 낼 주문을 paper/rehearsal 에 기록 (주문 없음)")
    args = ap.parse_args()

    load_env()
    cfg = TraderConfig(aggr_share=args.aggr_share, **STRATEGIES[args.strategy], **safety_limits())
    if args.rehearsal:
        rehearse(cfg)
        return
    if args.mode == "sim":
        broker = SimBroker(cfg.state_dir / "sim_account.json", starting_cash=args.sim_cash)
    else:
        broker = KiwoomBroker.from_env(mock=True, exchanges=EXCHANGES)

    mode = "simulate" if args.mode == "sim" else "kiwoom_mock"
    from quant import notify
    status = Runner(broker, cfg, mode=mode, notify=notify.send).run(execute=not args.check)
    print(f"전략: {status['strategy']['name']}  모드: {status['mode']}")
    print(f"신호 기준일: {status['signal']['as_of']}  마지막 리밸런싱: {status['last_rebalance']}")
    print(f"현금: {status['cash']}")
    for p in status["positions"]:
        print(f"  보유 {p['symbol']:5} {p['qty']:>5}주  평균단가 {p['avg_price']}")
    for a in status["alerts"]:
        print(f"  [{a['level']}] {a['message']}")
    print("자세한 상태: paper/state.json")
    if not args.no_dashboard:
        build_dashboard(cfg.state_dir, open_browser=args.open)


EXCHANGES = {"SPY": "NY", "IWM": "NY", "EFA": "NY", "GLD": "NY", "IEF": "ND",
             "TLT": "ND", "QQQ": "ND", "TQQQ": "ND", "UPRO": "NY",
             "SPYM": "NY", "QQQM": "ND", "GLDM": "NY", "VTWO": "ND"}

# Cheaper twins (same index, lower share price) traded with a small app budget
# (user's choice 2026-11-02). Signals still use the originals' long history.
# SPYM is the former SPLG (renamed 2025-10-31). Prices around 2026-11:
# SPY $755 -> SPYM ~$89, QQQ $690 -> QQQM ~$284, GLD $356 -> GLDM ~$72,
# IWM $279 -> VTWO ~$112. EFA, IEF, TLT, TQQQ and UPRO are already cheap.
CHEAP_TWINS = {"SPY": "SPYM", "QQQ": "QQQM", "GLD": "GLDM", "IWM": "VTWO"}


def rehearse(cfg: TraderConfig) -> None:
    """Live-account rehearsal: read the real account with the live keys and
    record what the strategy would order there. Orders are impossible twice
    over: the broker is read-only and the run is a check (execute=False).
    Records go to paper/rehearsal, apart from the paper-trading records."""
    from dataclasses import replace
    key, secret = os.getenv("KIWOOM_LIVE_APP_KEY"), os.getenv("KIWOOM_LIVE_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(".env 에 KIWOOM_LIVE_APP_KEY, KIWOOM_LIVE_SECRET_KEY 가 없어 리허설을 건너뜁니다.")
    broker = ReadOnlyBroker(KiwoomBroker(key, secret, mock=False, exchanges=EXCHANGES))
    cfg = replace(cfg, state_dir=Path(cfg.state_dir) / "rehearsal")
    broker = app_budget(broker, Path(cfg.state_dir))
    if broker.__class__.__name__ == "BudgetBroker":
        # A small budget splits into ~$45 pieces; the $50 skip rule would drop
        # most of them (whole-share sim 2015~: 1M KRW 7.3%/yr at $10 vs -0.2% at $50),
        # and cheaper twins fit whole shares better (12.4%/yr at 1M KRW).
        cfg = replace(cfg, min_trade_usd=min(cfg.min_trade_usd, SMALL_MIN_TRADE), trade_as=dict(CHEAP_TWINS))
    status = Runner(broker, cfg, mode="kiwoom_live").run(execute=False)
    print(f"리허설 (주문 없음): 현금 {status['cash']}, 예정 주문 {len(status.get('planned_orders') or [])}건")
    for a in status["alerts"]:
        print(f"  [{a['level']}] {a['message']}")


SMALL_MIN_TRADE = 10.0


def app_budget(broker, state_dir: Path, env=None):
    """Limit the app to its own slice of a shared real account (.env APP_BUDGET_KRW
    or APP_BUDGET_USD): it counts only the cash and shares it traded itself.
    Without a budget the whole account is the app's, as on the mock account.
    Raising or lowering the number in .env later adds or takes out the difference."""
    from quant.trader.budget import BudgetBroker, budget_setting
    from quant.trader.runner import fetch_usdkrw
    env = os.environ if env is None else env
    ledger = state_dir / "budget.json"
    setting = budget_setting(env)
    if not setting:
        return BudgetBroker(broker, ledger) if ledger.exists() else broker
    b = BudgetBroker(broker, ledger, setting=setting, usdkrw=fetch_usdkrw)
    if b.changed:
        print(b.changed)
    return b


def safety_limits(env=None) -> dict:
    """Order limits from .env: MAX_ORDER_USD (one order, dollars) and
    MAX_DAILY_LOSS_PCT (skip the day's orders after a drop this big, percent;
    0 turns it off). Blank or unreadable values keep the defaults."""
    env = os.environ if env is None else env
    out = {}
    try:
        if env.get("MAX_ORDER_USD", "").strip():
            out["max_order_usd"] = float(env["MAX_ORDER_USD"].replace(",", "").replace("$", ""))
    except ValueError:
        print("[경고] .env 의 MAX_ORDER_USD 를 숫자로 읽지 못해 기본값을 씁니다.")
    try:
        if env.get("MAX_DAILY_LOSS_PCT", "").strip():
            pct = float(env["MAX_DAILY_LOSS_PCT"].replace("%", ""))
            out["max_daily_loss"] = pct / 100 if pct > 0 else None
    except ValueError:
        print("[경고] .env 의 MAX_DAILY_LOSS_PCT 를 숫자로 읽지 못해 기본값(10%)을 씁니다.")
    return out


def build_dashboard(data_dir: Path, open_browser: bool = False) -> None:
    """Refresh the Mintory dashboard from the files this run just wrote.
    A dashboard problem must never look like a trading failure, so errors
    are printed and swallowed."""
    try:
        from dashboard import build
    except ImportError:
        print("[안내] dashboard 폴더가 없어 대시보드는 건너뜁니다.")
        return
    try:
        argv = ["--data", str(data_dir)] + ([] if open_browser else ["--no-open"])
        build.main(argv)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 대시보드를 만들지 못했습니다: {e}")


if __name__ == "__main__":
    main()
