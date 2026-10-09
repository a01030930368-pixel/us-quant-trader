# 대시보드 데이터 형식 (schema 1)

실행기(`scripts/run_trader.py`)는 실행할 때마다 `state/` 폴더에 아래 파일을 씁니다.
대시보드는 이 파일들을 **읽기만** 하면 됩니다. 주문은 실행기만 냅니다.

## state/status.json — 현재 상태 (매 실행마다 덮어씀)

```json
{
  "schema": 1,
  "updated_at": "2026-10-08T14:00:00+00:00",
  "mode": "sim | kiwoom",
  "strategy": "혼합 50/50 모멘텀",
  "halted": false,
  "note": "리밸런싱 실행 | 이번 달 리밸런싱 완료, 대기 중 | STOP 파일이 있어 매매를 멈췄습니다. | 한도 초과 주문 차단: ...",
  "last_close": "2026-10-07",
  "signal_date": "2026-09-30",
  "next_rebalance": "다음 달 첫 거래일",
  "equity_usd": 10012.5,
  "cash_usd": 412.3,
  "holdings": [
    {"ticker": "QQQ", "qty": 10, "price": 480.1, "value": 4801.0, "weight": 0.4795}
  ],
  "target_weights": {"QQQ": 0.5, "TQQQ": 0.25, "GLD": 0.25},
  "planned_orders": [
    {"ticker": "QQQ", "side": "buy", "qty": 10, "ref_price": 480.1, "value": 4801.0, "target_qty": 10}
  ],
  "order_results": [
    {"time": "...", "signal_date": "2026-09-30", "mode": "sim", "ticker": "QQQ", "side": "buy",
     "qty": 10, "ref_price": 480.1, "ok": true, "order_no": "SIM-...", "filled_qty": 10,
     "fill_price": 480.1, "fee": 7.2}
  ]
}
```

- `halted: true`면 매매가 멈춘 상태입니다. 이유는 `note`에 있습니다. 화면에 경고로 크게 보여주세요.
- `equity_usd`, `cash_usd`, `holdings`는 `halted`일 때 `null` 또는 빈 값일 수 있습니다.

## state/equity.csv — 일별 계좌 가치 (수익률 차트용)

```
date,equity,cash
2026-10-07,10012.5,412.3
```

하루에 한 줄씩 쌓이고, 같은 날짜로 다시 실행하면 그 줄을 덮어씁니다.

## state/trades.jsonl — 주문 기록 (한 줄에 주문 1건, 계속 추가)

필드는 `status.json`의 `order_results` 항목과 같습니다. 실패한 주문은 `ok: false`와 `msg`가 들어갑니다.
