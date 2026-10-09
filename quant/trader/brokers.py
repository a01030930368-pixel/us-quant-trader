"""Brokers the runner can trade through.

Every broker exposes the same small surface so the runner never cares which
one it talks to:

    positions() -> {ticker: shares}
    cash_usd() -> float
    place_order(ticker, side, qty, ref_price) -> dict   (side: "buy" | "sell")
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


class SimBroker:
    """Fills every order instantly at `ref_price` and keeps the account in a
    local JSON file. Used for rehearsal before touching any real API."""

    def __init__(self, state_path: Path, starting_cash: float = 10_000.0,
                 cost_bps: float = 15.0):
        self.path = Path(state_path)
        self.cost = cost_bps / 10_000
        if self.path.exists():
            self.acct = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.acct = {"cash": starting_cash, "positions": {}}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.acct, indent=2), encoding="utf-8")

    def positions(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.acct["positions"].items() if v}

    def cash_usd(self) -> float:
        return float(self.acct["cash"])

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float) -> dict:
        value = qty * ref_price
        fee = value * self.cost
        pos = self.acct["positions"]
        if side == "buy":
            if value + fee > self.acct["cash"] + 1e-6:
                return {"ok": False, "msg": "예수금 부족"}
            self.acct["cash"] -= value + fee
            pos[ticker] = pos.get(ticker, 0) + qty
        else:
            if pos.get(ticker, 0) < qty:
                return {"ok": False, "msg": "보유수량 부족"}
            self.acct["cash"] += value - fee
            pos[ticker] -= qty
        self._save()
        return {"ok": True, "order_no": f"SIM-{int(time.time() * 1000)}",
                "filled_qty": qty, "fill_price": ref_price, "fee": fee}


class KiwoomBroker:
    """Kiwoom REST API (US stocks). mock=True uses the paper-trading server.

    Token issue and quotes follow the official guide (au10001, usa20100).
    Balance/deposit/order TRs (ust21070, ust21160, ust20000, ust20001) are
    filled in from the guide pages; until then they raise NotImplementedError
    so nothing is ever sent by guesswork.
    """

    REAL = "https://api.kiwoom.com"
    MOCK = "https://mockapi.kiwoom.com"
    EXCHANGES = {"NASDAQ": "ND", "NYSE": "NY", "AMEX": "NA"}

    def __init__(self, appkey: str, secret: str, mock: bool = True,
                 exchanges: dict[str, str] | None = None):
        self.base = self.MOCK if mock else self.REAL
        self.appkey, self.secret = appkey, secret
        self.exchanges = exchanges or {}
        self._token: str | None = None
        self._token_exp = 0.0

    @classmethod
    def from_env(cls, mock: bool = True, **kw) -> "KiwoomBroker":
        appkey, secret = os.getenv("KIWOOM_APP_KEY"), os.getenv("KIWOOM_SECRET_KEY")
        if not appkey or not secret:
            raise RuntimeError(".env 에 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 가 없습니다.")
        return cls(appkey, secret, mock=mock, **kw)

    # -- plumbing ---------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(f"{self.base}/oauth2/token",
                          headers={"Content-Type": "application/json;charset=UTF-8"},
                          json={"grant_type": "client_credentials",
                                "appkey": self.appkey, "secretkey": self.secret},
                          timeout=15)
        body = r.json()
        if not body.get("token"):
            raise RuntimeError(f"토큰 발급 실패: {body}")
        self._token = body["token"]
        self._token_exp = _parse_expiry(body.get("expires_dt"))
        return self._token

    def call(self, path: str, api_id: str, body: dict, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            r = requests.post(f"{self.base}{path}",
                              headers={"Content-Type": "application/json;charset=UTF-8",
                                       "authorization": f"Bearer {self.token()}",
                                       "api-id": api_id},
                              json=body, timeout=15)
            data = r.json() if r.content else {}
            if r.ok and str(data.get("return_code", "0")) == "0":
                return data
            if attempt < retries and r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{api_id} 실패 (HTTP {r.status_code}): {data}")
        raise AssertionError("unreachable")

    def exchange_of(self, ticker: str) -> str:
        return self.exchanges.get(ticker, "ND")

    # -- market data ------------------------------------------------------
    def quote(self, ticker: str) -> dict:
        return self.call("/api/us/mrkcond", "usa20100",
                         {"stex_tp": self.exchange_of(ticker), "stk_cd": ticker})

    # -- account / orders (spec pending) -----------------------------------
    def positions(self) -> dict[str, int]:
        raise NotImplementedError("ust21070 명세 반영 전입니다.")

    def cash_usd(self) -> float:
        raise NotImplementedError("ust21160 명세 반영 전입니다.")

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float) -> dict:
        raise NotImplementedError("ust20000/ust20001 명세 반영 전입니다.")


def _parse_expiry(value: str | None) -> float:
    """expires_dt looks like 20261010235959; fall back to 12h."""
    try:
        dt = datetime.strptime(str(value), "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc).timestamp() - 9 * 3600  # KST
    except (TypeError, ValueError):
        return time.time() + 12 * 3600
