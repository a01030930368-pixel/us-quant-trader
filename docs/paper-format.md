# 모의투자 기록 형식 (대시보드용)

모의투자 실행기는 `paper/` 폴더에 아래 세 파일을 남깁니다. 대시보드(`dashboard/build.py`)는 이 파일을 **읽기만** 합니다.
실제 예시는 `dashboard/sample/`에 있습니다 (`python dashboard/make_sample.py`로 다시 만들 수 있음).

| 파일 | 쓰는 방식 | 내용 |
|---|---|---|
| `paper/state.json` | 실행할 때마다 통째로 덮어쓰기 | 지금 상태: 현금, 보유 종목, 목표 비중, 다음 리밸런싱, 최근 신호 |
| `paper/trades.jsonl` | 체결(또는 거부)마다 한 줄 추가 | 매매 기록 |
| `paper/equity.jsonl` | 거래일마다 한 줄 추가 | 일별 평가금액과 벤치마크(SPY) 종가 |

- 인코딩은 UTF-8, 날짜는 `YYYY-MM-DD`, 시각은 시간대가 붙은 ISO 8601(`2026-10-01T22:30:04+09:00`)입니다.
- 금액은 모두 계좌 통화(`base_currency`, 기본 USD) 기준이고, 수수료를 뺀 값입니다.
- `state.json`은 임시 파일에 쓴 뒤 이름을 바꿔(atomic replace) 반쯤 쓰인 파일이 읽히지 않게 해 주세요.
- 형식이 바뀌면 `schema_version`을 올립니다. 모르는 필드는 대시보드가 무시하므로 필드 추가는 버전을 올리지 않아도 됩니다.

## state.json

```json
{
  "schema_version": 1,
  "strategy": {
    "id": "momentum_blend_multi",
    "name": "혼합 50/50 멀티기간 모멘텀",
    "description": "한 줄 설명 (선택)",
    "sleeves": [
      {"id": "stable", "name": "안정 바구니", "weight": 0.5, "universe": ["SPY", "QQQ", "IWM", "EFA", "GLD"]},
      {"id": "aggressive", "name": "공격 바구니", "weight": 0.5, "universe": ["TQQQ", "UPRO", "GLD", "TLT"]}
    ],
    "safe_asset": "IEF"
  },
  "mode": "simulate",
  "base_currency": "USD",
  "benchmark": "SPY",
  "started_at": "2026-04-01",
  "initial_capital": 10000.0,
  "updated_at": "2026-10-08T06:10:00+09:00",
  "cash": 122.6,
  "positions": [
    {"symbol": "SPY", "qty": 10, "avg_price": 530.33, "last_price": 520.96, "sleeve": "stable"}
  ],
  "targets": [
    {"symbol": "SPY", "weight": 0.5, "sleeve": "stable"},
    {"symbol": "TQQQ", "weight": 0.25, "sleeve": "aggressive"}
  ],
  "last_rebalance": "2026-10-01",
  "next_rebalance": {"signal_date": "2026-10-30", "order_date": "2026-11-02"},
  "signal": {
    "as_of": "2026-09-30",
    "lookback_days": 252,
    "lookbacks": [21, 63, 126, 252],
    "scores": [
      {"symbol": "SPY", "sleeve": "stable", "return": 0.0663, "selected": true}
    ]
  },
  "alerts": [
    {"level": "warn", "message": "TQQQ 매수 주문이 거부되었습니다 (예수금 부족)"}
  ]
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `mode` | ✓ | `simulate`(가상 체결) · `kiwoom_mock`(키움 모의) · `kiwoom_live`(실계좌) |
| `initial_capital` | ✓ | 시작 원금. 누적 수익률의 기준 |
| `updated_at` | ✓ | 마지막 실행 시각. 4일 넘게 안 바뀌면 대시보드가 경고 표시 |
| `cash`, `positions` | ✓ | 키움 잔고 조회 결과(또는 가상 장부). `last_price`는 마지막 종가 |
| `positions[].sleeve` | | 한 종목이 두 바구니에 걸치면 `targets`를 기준으로 표시하므로 생략 가능 |
| `targets` | | 지금 목표 비중. 같은 종목이 두 바구니에 있으면 두 줄 |
| `next_rebalance` | | `signal_date` = 신호 계산일(월말 장 마감), `order_date` = 주문일 |
| `signal.scores` | | 바구니별 후보의 점수(`return`)와 선택 여부. `lookbacks`가 있으면 그 기간들 수익률의 평균(평균 모멘텀) |
| `signal.lookbacks` | | 점수에 쓴 기간(거래일). 없으면 `lookback_days` 한 기간 수익률로 표시 |
| `alerts` | | `level`: `info` · `warn` · `error` |

## trades.jsonl (한 줄 = 주문 하나)

```json
{"time": "2026-10-01T22:30:04+09:00", "date": "2026-10-01", "symbol": "IWM", "side": "sell", "qty": 27, "price": 206.71, "fee": 13.95, "status": "filled", "order_id": "S1020", "sleeve": "stable", "note": "월간 리밸런싱"}
```

- `side`: `buy` · `sell`
- `status`: `filled` · `partial` · `pending` · `rejected` · `cancelled` (거부·취소면 `message`에 사유)
- `date`는 미국 거래일 기준, `time`은 실제 주문 시각입니다.

## equity.jsonl (한 줄 = 거래일 하나)

```json
{"date": "2026-10-08", "equity": 10575.44, "cash": 122.6, "benchmark_close": 519.89}
```

- `equity` = 현금 + 보유 종목 평가금액 (그날 종가 기준)
- `benchmark_close`는 `state.benchmark`(SPY)의 그날 종가. 대시보드가 첫날 대비로 바꿔 비교합니다.

## agent.json (선택)

자동 실행 도우미(`scripts/mintory_agent.py`)가 쓰는 상태 파일입니다. 있으면 대시보드 상단에 "다음 자동 실행 10/12 23:45 · 마지막 실행 OK"처럼 표시합니다.

```json
{"agent": "mintory_agent", "next_run": "2026-10-12T23:45:00+09:00", "last_run": "2026-10-09T23:45:02+09:00", "last_result": "ok", "checked_at": "2026-10-10T08:00:00+09:00"}
```

- `last_result`: `ok`, 실패하면 `error (exit N)`. 실패면 빨간색으로 표시합니다.
- 대시보드는 실제 기록(`paper/`)을 볼 때 5분마다 화면을 새로 읽습니다.

