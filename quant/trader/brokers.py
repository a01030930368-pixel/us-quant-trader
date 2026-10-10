"""Brokers the runner can trade through.

Every broker exposes the same small surface so the runner never cares which
one it talks to:

    positions() -> {ticker: {"qty": shares, "avg_price": average cost}}
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
            self.acct = {"cash": starting_cash, "positions": {}, "avg_price": {}}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.acct, indent=2), encoding="utf-8")

    def positions(self) -> dict[str, dict]:
        avg = self.acct.setdefault("avg_price", {})
        return {k: {"qty": int(v), "avg_price": float(avg.get(k, 0.0))}
                for k, v in self.acct["positions"].items() if v}

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
            avg = self.acct.setdefault("avg_price", {})
            held = pos.get(ticker, 0)
            avg[ticker] = (held * avg.get(ticker, 0.0) + value) / (held + qty)
            pos[ticker] = held + qty
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
    Orders use ust20000/ust20001, holdings ust21070 and USD cash ust21160,
    with fields taken from the official guide pages.
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
        self._last_call = 0.0

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

    MIN_GAP = 1.0  # seconds between requests; Kiwoom rejects bursts
    LIMIT_SLIP = 0.015  # marketable-limit buffer; the runner keeps 2% cash

    def call(self, path: str, api_id: str, body: dict, retries: int = 3) -> dict:
        for attempt in range(retries + 1):
            wait = self._last_call + self.MIN_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            try:
                r = requests.post(f"{self.base}{path}",
                                  headers={"Content-Type": "application/json;charset=UTF-8",
                                           "authorization": f"Bearer {self.token()}",
                                           "api-id": api_id},
                                  json=body, timeout=30)
            except (requests.Timeout, requests.ConnectionError) as e:
                # The mock server sometimes stalls. Orders pass retries=0, so an
                # order that may have gone through is never sent twice.
                if attempt < retries:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError(f"{api_id} 응답 없음 (키움 서버): {e}") from e
            data = r.json() if r.content else {}
            if r.ok and str(data.get("return_code", "0")) == "0":
                return data
            msg = str(data.get("return_msg", ""))
            # 429 / "초과": rate limit. "지연"/"재시도" (e.g. RC9002, mock server
            # busy) or "Timeover response" (RC20): Kiwoom asks to retry later.
            # None of these means the request ran.
            busy = r.status_code == 429 or any(
                k in msg for k in ("초과", "지연", "재시도", "Timeover", "timeover"))
            # An expired or revoked token is refused before the request runs:
            # get a new one and send again.
            if attempt < retries and (r.status_code == 401 or "토큰" in msg or "token" in msg.lower()):
                self._token, self._token_exp = None, 0.0
                continue
            if attempt < retries and (r.status_code >= 500 or busy):
                time.sleep((10 if busy else 2) * (attempt + 1))
                continue
            raise RuntimeError(f"{api_id} 실패 (HTTP {r.status_code}): {data}")
        raise AssertionError("unreachable")

    def exchange_of(self, ticker: str) -> str:
        return self.exchanges.get(ticker, "ND")

    # -- market data ------------------------------------------------------
    def quote(self, ticker: str) -> dict:
        return self.call("/api/us/mrkcond", "usa20100",
                         {"stex_tp": self.exchange_of(ticker), "stk_cd": ticker})

    # -- account / orders ------------------------------------------------
    def positions(self) -> dict[str, dict]:
        """ust21070 on /api/us/acnt; empty body means every holding.

        Uses poss_qty (보유수량), which includes shares bought but not yet
        settled, so a fill from yesterday counts right away."""
        data = self.call("/api/us/acnt", "ust21070", {"stex_tp": "", "stk_cd": ""})
        out: dict[str, dict] = {}
        for row in data.get("result_list") or []:
            qty = int(_num(row.get("poss_qty")))
            if qty:
                out[row["stk_cd"].strip()] = {"qty": qty,
                                              "avg_price": _num(row.get("frgn_stk_book_uv"))}
        return out

    def cash_usd(self) -> float:
        """ust21160 on /api/us/acnt (no request fields).

        positions() counts unsettled shares, so cash must also be the amount
        after pending settlements: the furthest-out D-day USD deposit
        (d4 -> d0) that the response fills in."""
        data = self.call("/api/us/acnt", "ust21160", {})
        for day in ("d4", "d3", "d2", "d1", "d0"):
            value = str(data.get(f"{day}_usd_fx_entr") or "").strip()
            if value:
                return _num(value)
        raise RuntimeError(f"ust21160 응답에 외화예수금이 없습니다: {data}")

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float,
                    limit_price: float | None = None) -> dict:
        """ust20000 (buy) / ust20001 (sell) on /api/us/ordr.

        Always a limit order (trde_tp 00): the mock server rejects market
        orders (RC4065 "모의투자 지정가 매매만 가능합니다"). Without an explicit
        `limit_price` the limit is set LIMIT_SLIP past `ref_price` (above for a
        buy, below for a sell) so it fills right away like a market order but
        never at a runaway price. Kiwoom only accepts the order here; the fill
        shows up later in the balance, so the result is recorded as "pending".
        """
        if not limit_price:
            slip = self.LIMIT_SLIP if side == "buy" else -self.LIMIT_SLIP
            limit_price = round(ref_price * (1 + slip), 2)
        body = {"stex_tp": self.exchange_of(ticker), "stk_cd": ticker,
                "ord_qty": str(int(qty)), "ord_uv": f"{limit_price:.2f}",
                "trde_tp": "00"}
        if side == "sell":
            body["stop_pric"] = ""
        api_id = "ust20000" if side == "buy" else "ust20001"
        try:
            data = self.call("/api/us/ordr", api_id, body, retries=0)
        except (RuntimeError, requests.RequestException, ValueError) as e:
            msg = str(e)[:300]
            if "응답 없음" in msg:
                # No reply: the order may still have been accepted. Not retried;
                # the next run re-plans from the actual balance.
                return {"ok": False, "status": "pending",
                        "msg": "키움 응답 없음 - 접수 여부 불확실, 다음 실행 때 잔고로 확인"}
            return {"ok": False, "msg": msg}
        return {"ok": True, "status": "pending", "order_no": data.get("ord_no"),
                "fill_price": ref_price, "fee": 0.0,
                "msg": data.get("return_msg", "")}


def _parse_expiry(value: str | None) -> float:
    """expires_dt looks like 20261010235959; fall back to 12h."""
    try:
        dt = datetime.strptime(str(value), "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc).timestamp() - 9 * 3600  # KST
    except (TypeError, ValueError):
        return time.time() + 12 * 3600


def _num(value) -> float:
    """Kiwoom sends numbers as strings, sometimes zero-padded or signed
    ("+000012.34"); blanks count as 0."""
    text = str(value or "").replace(",", "").strip()
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


class ReadOnlyBroker:
    """Wraps a broker so it can read the account but never order.

    Used by the live-account rehearsal: real balances, no real orders."""

    def __init__(self, inner):
        self.inner = inner

    def positions(self) -> dict[str, dict]:
        return self.inner.positions()

    def cash_usd(self) -> float:
        return self.inner.cash_usd()

    def place_order(self, *a, **kw) -> dict:
        raise RuntimeError("리허설 중에는 주문을 내지 않습니다.")
