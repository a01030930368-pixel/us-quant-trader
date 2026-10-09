"""키움 REST API 미국주식 모의투자 지원 여부 확인.

1단계: 모의투자 서버(mockapi.kiwoom.com)에서 접근토큰 발급 -> 모의투자 앱키가 맞는지 확인
2단계: 미국주식 현재가 조회(usa20100)를 모의투자 서버로 호출 -> 미국주식이 모의에서 되는지 확인

주문은 보내지 않습니다(조회만).

준비:
  .env 파일에 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 입력 (모의투자용 키)

실행:
  python scripts/kiwoom_mock_check.py --ticker AAPL --exchange ND
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

MOCK = "https://mockapi.kiwoom.com"


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def get_token(appkey: str, secret: str) -> str:
    r = requests.post(f"{MOCK}/oauth2/token",
                      headers={"Content-Type": "application/json;charset=UTF-8"},
                      json={"grant_type": "client_credentials",
                            "appkey": appkey, "secretkey": secret},
                      timeout=15)
    body = r.json() if r.content else {}
    token = body.get("token")
    if not token:
        print("[실패] 토큰 발급 실패. 모의투자용 앱키/시크릿이 맞는지 확인하세요.")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        sys.exit(1)
    print("[성공] 1단계: 모의투자 토큰 발급 OK")
    return token


def find_exchanges(token: str, path: str, api_id: str) -> None:
    tickers = ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT", "IEF", "TQQQ", "UPRO"]
    print("\n티커별 거래소 코드:")
    for t in tickers:
        found = []
        for ex in ("ND", "NY", "NA"):
            r = requests.post(f"{MOCK}{path}",
                              headers={"Content-Type": "application/json;charset=UTF-8",
                                       "authorization": f"Bearer {token}", "api-id": api_id},
                              json={"stex_tp": ex, "stk_cd": t}, timeout=15)
            try:
                ok = r.ok and str(r.json().get("return_code")) == "0"
            except ValueError:
                ok = False
            if ok:
                found.append(ex)
        print(f"  {t:5} -> {', '.join(found) if found else '찾지 못함'}")
    print("\n이 결과를 Claude에게 그대로 붙여 주세요.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="/api/us/mrkcond",
                    help="usa20100 URL 경로 (API 가이드 기준 /api/us/mrkcond)")
    ap.add_argument("--api-id", default="usa20100")
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--exchange", default="ND",
                    help="거래소 구분: ND=NASDAQ, NY=NYSE, NA=AMEX")
    ap.add_argument("--body", default=None,
                    help='요청 본문 JSON을 직접 지정 (가이드의 필드명이 다를 때). 예: \'{"stk_cd":"AAPL"}\'')
    ap.add_argument("--find-exchange", action="store_true",
                    help="실행기가 쓰는 ETF마다 맞는 거래소 코드(ND/NY/NA)를 찾아 출력")
    args = ap.parse_args()

    load_env()
    appkey, secret = os.getenv("KIWOOM_APP_KEY"), os.getenv("KIWOOM_SECRET_KEY")
    if not appkey or not secret:
        print(".env 에 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 를 넣어주세요 (.env.example 참고).")
        sys.exit(1)

    token = get_token(appkey, secret)
    if args.find_exchange:
        find_exchanges(token, args.path, args.api_id)
        return
    body = json.loads(args.body) if args.body else {"stex_tp": args.exchange, "stk_cd": args.ticker}
    r = requests.post(f"{MOCK}{args.path}",
                      headers={"Content-Type": "application/json;charset=UTF-8",
                               "authorization": f"Bearer {token}",
                               "api-id": args.api_id},
                      json=body, timeout=15)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    print(f"\nHTTP {r.status_code}")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    rc = str(data.get("return_code", ""))
    if r.ok and rc in ("0", ""):
        print("\n[성공] 2단계: 미국주식 조회가 모의투자 서버에서 동작합니다.")
    else:
        print("\n[확인 필요] 위 응답 메시지를 Claude에게 그대로 보여주세요."
              " '모의투자 미지원' 류의 메시지면 미국주식 모의는 안 되는 것입니다.")


if __name__ == "__main__":
    main()
