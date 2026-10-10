# us-quant-trader

미국주식 퀀트 자동매매 프로젝트입니다.

진행 순서: 백테스트로 전략 순위 매기기 → 모의투자 약 1개월 → 소액 실계좌.

## 구성

| 경로 | 내용 |
|---|---|
| `quant/data.py` | 야후 파이낸스 일봉 가격 다운로드 (캐시) |
| `quant/strategies.py` | 전략: 보유, 이동평균 추세, 모멘텀 로테이션, RSI2 평균회귀 |
| `quant/backtest.py` | 목표 비중 기반 백테스트 (거래비용 반영, 미래 데이터 사용 방지) |
| `quant/metrics.py` | CAGR, Sharpe, Sortino, MDD, Calmar |
| `quant/ranking.py` | 학습(IS)/검증(OOS) 분리, 검증 구간 기준 순위, 파라미터 안정성 |
| `scripts/run_backtest.py` | 모든 후보 전략 백테스트 후 `reports/ranking.md` 생성 |
| `scripts/kiwoom_mock_check.py` | 키움 REST 미국주식 모의투자 지원 여부 확인 (조회만) |
| `dashboard/` · `mintory.bat` | Mintory 모의투자 대시보드 (수익률 vs SPY, 보유 종목, 매매 내역, 다음 리밸런싱) |
| `docs/paper-format.md` | 실행기가 남기는 기록 파일 형식 |

## 실행 방법

```bash
pip install -r requirements.txt
python scripts/run_backtest.py          # 백테스트 + 순위표
pytest -q                               # 테스트
```

PC 설치 없이 돌리려면 GitHub의 **Actions → backtest → Run workflow**를 누르면 됩니다.

## Mintory 대시보드

`mintory.bat`을 더블클릭하면 키움 모의계좌 잔고를 새로 읽고(주문은 내지 않음) 브라우저에 대시보드가 열립니다. Mintory를 한 번 열면 매일 자동 실행도 앱이 알아서 합니다(`docs/trader-guide.md` 참고).

```bash
python dashboard/build.py            # paper/ 폴더의 기록으로 보기 (기록이 없으면 샘플)
python dashboard/build.py --sample   # 샘플 데이터로 보기
```

결과 파일은 `reports/dashboard.html`에 저장됩니다. 브라우저에서 F11(전체화면)으로 보면 한 화면에 모두 들어옵니다. 기록 형식은 `docs/paper-format.md`를 보세요.

새 버전이 나오면 `update.bat`을 더블클릭하세요. `.env`와 `paper/` 기록은 그대로 두고 코드만 최신으로 바꿉니다(처음 한 번 GitHub 토큰 필요, `docs/trader-guide.md` 5번).

휴대폰으로 매매 알림을 받으려면 `notify_setup.bat`을 한 번 더블클릭해 텔레그램 봇을 연결하세요(`docs/trader-guide.md` 6번).

바탕화면에 Mintory 아이콘을 만들려면 `create_shortcut.bat`을 한 번 더블클릭하세요. 아이콘으로 열면 주소창 없는 앱 창으로 뜹니다.

## 순위 규칙

- 2018-01-01 이전은 학습(IS), 이후는 검증(OOS) 구간입니다. 순위는 **검증 구간 성과**로 매깁니다.
- 점수 = Sharpe 40% + Calmar 30% + CAGR 20% + 파라미터 안정성 10% (각 지표의 백분위 순위).
- MDD가 -30%보다 깊거나 거래가 10회 미만이면 탈락(`passed=False`)입니다.
- 학습 구간 성과가 검증 구간보다 훨씬 좋으면 과최적화를 의심하세요.

## 주의

- API 키는 `.env`에만 넣고 절대 커밋하지 마세요 (`.gitignore`에 포함됨).
- 야후 데이터는 상장폐지 종목이 빠져 있어 결과가 실제보다 좋게 나올 수 있습니다. 현재 후보는 ETF 위주라 영향이 작습니다.
