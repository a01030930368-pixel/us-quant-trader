"""Whether Kiwoom accepts this PC's internet address (IP).

Kiwoom's REST API only answers PCs whose IP is registered on its site
(openapi.kiwoom.com > API 사용신청). On Wi-Fi like a company dorm the IP
changes on every reconnect, and then Kiwoom refuses with 8050 (or 8010/8040/
8103) before any order. The broker writes what it saw to paper/kiwoom_ip.json:
Mintory shows a bar at the top and Telegram /status shows a line. No phone
alert is sent (the owner's choice, 2026-11-04). The next successful call to
the same server clears it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

STATUS = "kiwoom_ip.json"
IP_CODES = ("8010", "8040", "8050", "8103")   # Kiwoom: IP / terminal not registered
SERVERS = {"real": "실계좌", "mock": "모의투자"}


def public_ip(fetch=None) -> str | None:
    """This PC's internet address as websites see it (what Kiwoom checks)."""
    try:
        if fetch is None:
            import requests
            def fetch():
                return requests.get("https://api.ipify.org", timeout=5).text
        ip = str(fetch()).strip()
        parts = ip.split(".")
        return ip if len(parts) == 4 and all(p.isdigit() and int(p) < 256 for p in parts) else None
    except Exception:  # noqa: BLE001 - the address is only a hint
        return None


def read(folder: Path) -> dict:
    try:
        return json.loads((Path(folder) / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def blocked(folder: Path) -> dict:
    """{server: record} for each server that currently refuses this PC."""
    return {k: v for k, v in read(folder).items() if isinstance(v, dict) and v.get("blocked")}


def record(folder: Path, server: str, blocked_now: bool, code: str = "", ip_source=None) -> None:
    """Remember the latest answer for one server ("real" or "mock")."""
    data = read(folder)
    rec = {"blocked": blocked_now, "time": datetime.now().astimezone().isoformat(timespec="seconds")}
    if blocked_now:
        rec["code"] = code
        rec["ip"] = public_ip(ip_source)
    elif not (data.get(server) or {}).get("blocked"):
        return  # nothing to clear: don't rewrite the file on every request
    data[server] = rec
    path = Path(folder) / STATUS
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def message(rec: dict, server: str) -> str:
    """One line for the screen and for /status."""
    ip = rec.get("ip")
    where = f"이 PC의 IP({ip})" if ip else "이 PC의 IP"
    return (f"키움 {SERVERS.get(server, server)} 접속이 막혔어요: {where}가 키움에 등록되지 않았어요"
            f" (오류 {rec.get('code', '')}). 키움 REST API 홈페이지 > API 사용신청에서 {ip or '지금 IP'}를 추가해 주세요."
            + (" 등록한 뒤 Mintory '실계좌 키 확인'을 누르면 이 안내가 사라져요." if server == "real" else ""))
