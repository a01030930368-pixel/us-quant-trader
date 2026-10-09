"""매일 한 번 실행하는 모의투자 실행기.

    python scripts/run_trader.py --mode sim        # 가상 계좌로 연습 (기본)
    python scripts/run_trader.py --mode kiwoom     # 키움 모의투자 서버
    python scripts/run_trader.py --check           # 주문 없이 계획만 확인

매매를 즉시 멈추려면 state 폴더에 STOP 이라는 빈 파일을 만드세요.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant.trader.brokers import KiwoomBroker, SimBroker  # noqa: E402
from quant.trader.runner import Runner, TraderConfig  # noqa: E402
from scripts.kiwoom_mock_check import load_env  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sim", "kiwoom"], default="sim")
    ap.add_argument("--check", action="store_true", help="주문 없이 계획만 출력")
    ap.add_argument("--aggr-share", type=float, default=0.5)
    ap.add_argument("--sim-cash", type=float, default=10_000.0, help="가상 계좌 시작금(USD)")
    args = ap.parse_args()

    cfg = TraderConfig(aggr_share=args.aggr_share)
    if args.mode == "sim":
        broker = SimBroker(cfg.state_dir / "sim_account.json", starting_cash=args.sim_cash)
    else:
        load_env()
        broker = KiwoomBroker.from_env(mock=True, exchanges={
            "SPY": "NY", "IWM": "NY", "EFA": "NY", "GLD": "NY", "IEF": "ND",
            "TLT": "ND", "QQQ": "ND", "TQQQ": "ND", "UPRO": "NY"})

    status = Runner(broker, cfg, mode=args.mode).run(execute=not args.check)
    print(json.dumps({k: status[k] for k in
                      ("note", "signal_date", "equity_usd", "cash_usd", "target_weights")},
                     ensure_ascii=False, indent=2))
    for o in status["planned_orders"]:
        print(f"  {o['side']:4} {o['ticker']:5} {o['qty']:>5}주 @ {o['ref_price']:.2f}")


if __name__ == "__main__":
    main()
