"""Mintory 자동 실행기: 앱이 켜져 있는 동안 매일 정해진 시각에 모의투자를 돌린다.

    pythonw scripts/mintory_agent.py                    # 창 없이 백그라운드로 (mintory.bat이 실행)
    python scripts/mintory_agent.py --install-startup   # 윈도우 시작 시 자동으로 켜지게 등록
    python scripts/mintory_agent.py --uninstall-startup # 자동 시작 해제
    python scripts/mintory_agent.py --status            # 다음 실행 시각 확인

- 한국시간 월~금 밤 23:45에 `run_trader.py --mode kiwoom`을 실행한다(미국장 중).
  리밸런싱 날에만 주문하고 나머지 날은 기록과 대시보드 갱신만 한다.
- 23:45 이후 새벽 5시 전에 켜졌는데 그날 실행이 아직이면 바로 실행한다.
- 한 번에 하나만 돈다(이미 떠 있으면 조용히 종료).
- 상태는 paper/agent.json, 로그는 paper/logs/agent.log.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
RUN_AT = (23, 45)          # KST, inside the US session in summer and winter time
CATCHUP_UNTIL = 5          # KST hour the US session ends
LOCK_PORT = 47321          # a bound localhost port = "agent already running"
STATE = ROOT / "paper" / "agent.json"
LOG = ROOT / "paper" / "logs" / "agent.log"
STARTUP_NAME = "Mintory agent.bat"


def session_day(now: datetime) -> str:
    """The KST evening a run belongs to: 00:00-04:59 counts as the previous day."""
    now = now.astimezone(KST)
    if now.hour < CATCHUP_UNTIL:
        now -= timedelta(days=1)
    return now.date().isoformat()


def is_trading_evening(day: str) -> bool:
    return datetime.fromisoformat(day).weekday() < 5  # Mon-Fri KST = US Mon-Fri


def due_now(now: datetime, last_day: str | None) -> bool:
    """True inside today's run window (23:45-04:59 KST) when it has not run yet."""
    now = now.astimezone(KST)
    day = session_day(now)
    in_window = now.hour < CATCHUP_UNTIL or (now.hour, now.minute) >= RUN_AT
    return in_window and is_trading_evening(day) and last_day != day


def next_run(now: datetime, last_day: str | None) -> datetime:
    now = now.astimezone(KST)
    if due_now(now, last_day):
        return now
    start = datetime.fromisoformat(session_day(now))
    for i in range(8):
        d = start + timedelta(days=i)
        t = d.replace(hour=RUN_AT[0], minute=RUN_AT[1], tzinfo=KST)
        if t > now and is_trading_evening(d.date().isoformat()) and d.date().isoformat() != last_day:
            return t
    raise AssertionError("unreachable")


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(KST).isoformat(timespec='seconds')} {msg}\n")


def run_trader() -> int:
    python = sys.executable.replace("pythonw.exe", "python.exe")
    with open(LOG, "a", encoding="utf-8") as out:
        proc = subprocess.run([python, str(ROOT / "scripts" / "run_trader.py"), "--mode", "kiwoom"],
                              cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode


def loop() -> None:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return  # another agent is already running
    log("자동 실행기 시작")
    while True:
        state = read_state()
        now = datetime.now(KST)
        if due_now(now, state.get("last_day")):
            log("모의투자 실행")
            code = run_trader()
            state.update(last_day=session_day(now), last_run=now.isoformat(timespec="seconds"),
                         last_result="ok" if code == 0 else f"error (exit {code})")
            log(f"모의투자 종료: {state['last_result']}")
        state.update(agent="running", checked_at=now.isoformat(timespec="seconds"),
                     next_run=next_run(datetime.now(KST), state.get("last_day"))
                     .isoformat(timespec="minutes"))
        write_state(state)
        time.sleep(60)


def startup_file() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / STARTUP_NAME)


def install_startup() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    target = startup_file()
    target.write_text("@echo off\r\n"
                      f'start "" "{pythonw}" "{ROOT / "scripts" / "mintory_agent.py"}"\r\n',
                      encoding="mbcs" if os.name == "nt" else "utf-8")
    return target


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-startup", action="store_true")
    ap.add_argument("--uninstall-startup", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.install_startup:
        print(f"윈도우 시작 시 자동 실행 등록: {install_startup()}")
    elif args.uninstall_startup:
        startup_file().unlink(missing_ok=True)
        print("자동 시작을 해제했습니다.")
    elif args.status:
        state = read_state()
        print(f"다음 자동 실행: {next_run(datetime.now(KST), state.get('last_day'))}")
        print(f"마지막 실행: {state.get('last_run')} ({state.get('last_result')})")
    else:
        loop()


if __name__ == "__main__":
    main()
