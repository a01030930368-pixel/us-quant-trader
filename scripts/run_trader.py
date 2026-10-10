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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.trader.brokers import KiwoomBroker, SimBroker  # noqa: E402
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
    args = ap.parse_args()

    cfg = TraderConfig(aggr_share=args.aggr_share, **STRATEGIES[args.strategy])
    if args.mode == "sim":
        broker = SimBroker(cfg.state_dir / "sim_account.json", starting_cash=args.sim_cash)
    else:
        load_env()
        broker = KiwoomBroker.from_env(mock=True, exchanges={
            "SPY": "NY", "IWM": "NY", "EFA": "NY", "GLD": "NY", "IEF": "ND",
            "TLT": "ND", "QQQ": "ND", "TQQQ": "ND", "UPRO": "NY"})

    mode = "simulate" if args.mode == "sim" else "kiwoom_mock"
    status = Runner(broker, cfg, mode=mode).run(execute=not args.check)
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
