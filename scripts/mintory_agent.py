"""Mintory 자동 실행기: 앱이 켜져 있는 동안 매일 정해진 시각에 모의투자를 돌리고,
Mintory 화면의 버튼(잔고 새로고침, 지금 매매 실행, 일시정지)을 처리한다.

    pythonw scripts/mintory_agent.py                    # 창 없이 백그라운드로 (mintory.bat이 실행)
    python scripts/mintory_agent.py --open              # Mintory 창 열기 (mintory.bat이 실행)
    python scripts/mintory_agent.py --install-startup   # 윈도우 시작 시 자동으로 켜지게 등록
    python scripts/mintory_agent.py --uninstall-startup # 자동 시작 해제
    python scripts/mintory_agent.py --status            # 다음 실행 시각 확인

- 한국시간 월~금 밤 23:45에 `run_trader.py --mode kiwoom`을 실행한다(미국장 중).
  리밸런싱 날에만 주문하고 나머지 날은 기록과 대시보드 갱신만 한다.
- 23:45 이후 새벽 5시 전에 켜졌는데 그날 실행이 아직이면 바로 실행한다.
- 놓친 매매 대비: 23:40에 PC를 잠자기에서 깨우는 윈도우 작업을 등록하고, 실행 전후로는
  PC가 잠들지 않게 붙잡는다. 그래도 놓친 날(PC가 꺼져 있던 밤)은 휴대폰으로 알리고,
  밀린 주문은 다음 실행이 목표 비중과 비교해 알아서 맞춘다.
- 화면은 http://127.0.0.1:47321 에서 보여준다. 이 주소는 이 PC 안에서만 열린다.
  같은 포트가 "이미 켜져 있음" 표시도 겸해서, 한 번에 하나만 돈다.
- 일시정지는 paper/STOP 파일을 만들고 지우는 것과 같다(실행기가 STOP이 있으면 주문을 안 냄).
- 텔레그램이 연결돼 있으면 휴대폰 명령(/balance, /refresh, /status, /pause, /resume)에 답한다
  (quant/telegram_bot.py). 주문을 내는 명령은 없다.
- 상태는 paper/agent.json, 로그는 paper/logs/agent.log.
- 이 파일이 바뀌면(update.bat 업데이트) 스스로 새 코드로 다시 켜진다.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KST = timezone(timedelta(hours=9))
RUN_AT = (23, 45)          # KST, inside the US session in summer and winter time
CATCHUP_UNTIL = 5          # KST hour the US session ends
PORT = 47321               # the dashboard server; a listener here = "agent already running"
LOCK_PORT = PORT
URL = f"http://127.0.0.1:{PORT}/"
PAPER = ROOT / "paper"
STATE = PAPER / "agent.json"
STOP = PAPER / "STOP"
LOG = PAPER / "logs" / "agent.log"
STARTUP_NAME = "Mintory agent.bat"
WAKE_TASK = "Mintory 자동 실행"
WAKE_AT = (23, 40)         # KST, a few minutes before RUN_AT
AWAKE_BEFORE = timedelta(minutes=20)
WATCHED = [Path(__file__).resolve(), ROOT / "dashboard" / "build.py",
           ROOT / "quant" / "telegram_bot.py", ROOT / "quant" / "notify.py"]

# What each button runs. Both use the Kiwoom mock account; there is no live mode here.
JOBS = {
    "refresh": ["--mode", "kiwoom", "--check"],   # read balances only, never orders
    "run": ["--mode", "kiwoom"],                  # the same run as the 23:45 schedule
}
JOB_NAMES = {"refresh": "잔고 새로고침", "run": "매매 실행", "auto": "자동 매매 실행"}


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


def missed_days(now: datetime, last_day: str | None, limit: int = 10) -> list[str]:
    """Trading evenings after last_day whose run window is over without a run."""
    if not last_day:
        return []
    today = datetime.fromisoformat(session_day(now))
    last = datetime.fromisoformat(last_day)
    days = []
    for i in range(1, limit + 1):
        d = today - timedelta(days=i)
        if d <= last:
            break
        if is_trading_evening(d.date().isoformat()):
            days.append(d.date().isoformat())
    return sorted(days)


def should_stay_awake(now: datetime, last_day: str | None) -> bool:
    """Hold the PC awake from shortly before the run until it has run."""
    return next_run(now, last_day) - now.astimezone(KST) <= AWAKE_BEFORE


def stay_awake(on: bool) -> None:
    """Ask Windows not to sleep while on (idle sleep only; a closed lid still sleeps)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        flags = 0x80000000 | (0x00000001 if on else 0)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:  # noqa: BLE001 - best effort
        pass


WEEKDAY_KO = "월화수목금토일"


def missed_message(days: list[str], next_iso: str | None) -> str:
    when = ", ".join(f"{int(d[5:7])}/{int(d[8:10])}({WEEKDAY_KO[datetime.fromisoformat(d).weekday()]})"
                     for d in days)
    nxt = ""
    if next_iso:
        t = datetime.fromisoformat(next_iso)
        nxt = f" ({t.month}/{t.day} {t:%H:%M})"
    return (f"⚠️ Mintory 자동 실행을 놓쳤어요\n\n🌙 {when} 밤\n"
            "그 시각에 PC가 꺼져 있었거나 잠자기 상태였어요.\n\n"
            f"🔁 밀린 주문은 다음 자동 실행{nxt} 때 알아서 맞춰집니다.\n"
            "💡 그 시각에 PC를 켜 두세요.")


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_state_lock = threading.Lock()


def write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def update_state(**changes) -> dict:
    with _state_lock:
        state = read_state()
        state.update(changes)
        write_state(state)
        return state


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {msg}\n")


def run_trader(args: list[str] = JOBS["run"]) -> int:
    python = sys.executable.replace("pythonw.exe", "python.exe")
    with open(LOG, "a", encoding="utf-8") as out:
        proc = subprocess.run([python, str(ROOT / "scripts" / "run_trader.py"), *args],
                              cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode


def count_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())
    except OSError:
        return 0


# -- jobs: one trader run at a time, whether from the schedule or a button ------
_job_lock = threading.Lock()
_job: dict = {"name": None, "started": None}


def _run_job(name: str) -> str:
    """Run a job while holding _job_lock; returns "ok" or an error text."""
    _job.update(name=name, started=now_iso())
    update_state(agent="trading" if name != "refresh" else "refreshing", job=name,
                 checked_at=_job["started"])
    log(f"{JOB_NAMES[name]} 시작")
    trades_before = count_lines(PAPER / "trades.jsonl")
    try:
        code = run_trader(JOBS.get(name, JOBS["run"]))
        result = "ok" if code == 0 else f"error (exit {code})"
    except Exception as e:  # noqa: BLE001 - a failed run must not kill the agent
        result = f"error ({e})"
    log(f"{JOB_NAMES[name]} 종료: {result}")
    if name != "refresh":
        from quant import notify
        if notify.notify_run(name, result, PAPER, trades_before):
            log("휴대폰 알림 보냄")
    update_state(agent="running", job=None,
                 last_job={"name": name, "finished": now_iso(), "result": result})
    _job.update(name=None, started=None)
    return result


def start_job(name: str) -> bool:
    """Start a button job in the background. False if another run is going."""
    if not _job_lock.acquire(blocking=False):
        return False

    def work():
        try:
            _run_job(name)
        finally:
            _job_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def set_paused(paused: bool) -> None:
    if paused:
        PAPER.mkdir(parents=True, exist_ok=True)
        STOP.write_text("Mintory 화면에서 일시정지함\n", encoding="utf-8")
        log("일시정지: 주문을 내지 않음")
    else:
        STOP.unlink(missing_ok=True)
        log("다시 시작: 주문을 다시 냄")


def control_state() -> dict:
    # Read the job before the state file: a job writes last_job first and only
    # then clears its name, so "not busy" here always comes with its last_job.
    busy, since = _job["name"], _job["started"]
    state = read_state()
    return {"paused": STOP.exists(), "busy": busy, "busy_since": since,
            "last_job": state.get("last_job"), "next_run": state.get("next_run")}


# -- the dashboard server (127.0.0.1 only) --------------------------------------
class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, which would
    # break "one agent at a time". Linux needs it to rebind right after a restart.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "Mintory"

    def log_message(self, *args) -> None:  # keep agent.log for trading, not page loads
        pass

    def _host_ok(self) -> bool:
        # Only pages opened at our own address; blocks DNS-rebinding tricks.
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        path = urlsplit(self.path).path
        if path == "/":
            try:
                from dashboard import build
                html = build.page(PAPER, extra={"control": control_state()}, quiet=True)
            except Exception as e:  # noqa: BLE001
                return self._send(500, f"대시보드를 만들지 못했습니다: {e}".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(200, control_state())
        if path == "/api/quotes":
            from quant import live
            symbols = live.clean_symbols(parse_qs(urlsplit(self.path).query).get("symbols", [""])[0])
            return self._json(200, live.quotes(symbols))
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        # The custom header forces a CORS preflight, which we never approve, so other
        # websites open in the browser cannot press these buttons.
        if not self._host_ok() or self.headers.get("X-Mintory") != "1":
            return self._json(403, {"error": "forbidden"})
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        path = urlsplit(self.path).path
        if path in ("/api/refresh", "/api/run"):
            name = path.rsplit("/", 1)[1]
            if not start_job(name):
                return self._json(409, {"error": f"{JOB_NAMES.get(_job['name'], '다른 작업')} 중이에요. "
                                                 "끝나면 다시 눌러주세요.", **control_state()})
            return self._json(200, control_state())
        if path == "/api/pause":
            set_paused(bool(body.get("paused", True)))
            return self._json(200, control_state())
        self._json(404, {"error": "not found"})


def start_server(wait: float = 0) -> Server | None:
    """Bind the dashboard port. None if another agent already has it."""
    deadline = time.monotonic() + wait
    while True:
        try:
            server = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def code_mtime() -> float:
    return sum(p.stat().st_mtime for p in WATCHED if p.exists())


def restart(server: Server) -> None:
    """Hand over to a fresh copy of this script running the updated code."""
    log("코드가 업데이트되어 자동 실행기를 다시 켬")
    server.shutdown()
    server.server_close()
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--restarted"], cwd=ROOT,
                     creationflags=flags, start_new_session=os.name != "nt")


def loop(restarted: bool = False) -> None:
    server = start_server(wait=150 if restarted else 0)
    if server is None:
        log("자동 실행기가 이미 켜져 있어 새로 켜지 않음")
        return
    log(f"자동 실행기 시작 (pid {os.getpid()}, 화면 {URL})")
    started = code_mtime()
    update_state(agent="running", job=None)
    start_bot()
    while True:
        if code_mtime() != started and _job_lock.acquire(blocking=False):
            stay_awake(False)
            restart(server)
            return
        state = read_state()
        now = datetime.now(KST)
        check_missed(now, state)
        stay_awake(should_stay_awake(now, state.get("last_day")))
        if due_now(now, state.get("last_day")):
            with _job_lock:  # waits for a button run to finish first
                result = _run_job("auto")
            update_state(last_day=session_day(now), last_run=now.isoformat(timespec="seconds"),
                         last_result=result)
            state = read_state()
        update_state(agent="running" if not _job["name"] else read_state().get("agent"),
                     checked_at=now_iso(),
                     next_run=next_run(datetime.now(KST), state.get("last_day"))
                     .isoformat(timespec="minutes"))
        time.sleep(60)


def check_missed(now: datetime, state: dict) -> list[str]:
    """Record missed evenings for the dashboard and send one phone alert per new miss."""
    days = missed_days(now, state.get("last_day"))
    new = [d for d in days if d > (state.get("missed_alerted") or "")]
    changes: dict = {}
    if days != state.get("missed", []):
        changes["missed"] = days
    if new:
        log("자동 실행을 놓침: " + ", ".join(new))
        from quant import notify
        nxt = next_run(now, state.get("last_day")).isoformat(timespec="minutes")
        if notify.send(missed_message(new, nxt)):
            log("휴대폰 알림 보냄")
        changes["missed_alerted"] = new[-1]
    if changes:
        update_state(**changes)
    return days


def refresh_now() -> str | None:
    """Balance check for the phone; None when another run holds the lock."""
    if not _job_lock.acquire(blocking=False):
        return None
    try:
        return _run_job("refresh")
    finally:
        _job_lock.release()


def start_bot() -> None:
    from quant import live, telegram_bot
    act = telegram_bot.Actions(
        state=lambda: _read_json(PAPER / "state.json"), agent=read_state,
        paused=lambda: STOP.exists(), set_paused=set_paused, refresh=refresh_now,
        quotes=lambda syms: live.quotes(live.clean_symbols(",".join(syms))))
    threading.Thread(target=telegram_bot.serve, daemon=True, kwargs={
        "act": act, "get_offset": lambda: read_state().get("tg_offset"),
        "set_offset": lambda n: update_state(tg_offset=n), "log": log}).start()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def agent_alive() -> bool:
    """True when an agent is listening on the dashboard port."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def port_taken() -> bool:
    """True when something holds the port without serving (an agent from before buttons)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", PORT))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def open_app() -> int:
    """Open the Mintory window from the agent and refresh balances (no orders).
    Falls back to the old file page when the agent does not come up."""
    print("Mintory를 여는 중...")
    deadline = time.monotonic() + 15
    while not agent_alive():
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    else:
        deadline = None
    if deadline is not None:
        if port_taken():
            print("예전 버전의 자동 실행기가 아직 켜져 있어 새 화면을 열 수 없어요.\n"
                  "PC를 재시작하거나, 작업 관리자 > 세부 정보에서 pythonw.exe를 끝낸 뒤 다시 열어주세요.")
        print("지금은 버튼 없는 화면으로 엽니다. 잔고를 읽는 중...")
        python = sys.executable.replace("pythonw.exe", "python.exe")
        return subprocess.call([python, str(ROOT / "scripts" / "run_trader.py"),
                                *JOBS["refresh"], "--open"], cwd=ROOT)
    try:
        req = urllib.request.Request(URL + "api/refresh", data=b"{}", method="POST",
                                     headers={"X-Mintory": "1", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except OSError:
        pass  # already busy, or an old agent without buttons: just open the page
    from dashboard import build
    build.open_url(URL)
    return 0


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


def wake_task_xml(pythonw: Path, script: Path) -> str:
    """Task Scheduler task: Mon-Fri 23:40 KST, wakes the PC and starts the agent
    (a second copy exits at once when one is already running)."""
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Mintory: 자동 매매 직전에 PC를 깨웁니다.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-05T{WAKE_AT[0]:02d}:{WAKE_AT[1]:02d}:00+09:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek><Monday /><Tuesday /><Wednesday /><Thursday /><Friday /></DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(pythonw))}</Command>
      <Arguments>"{escape(str(script))}"</Arguments>
      <WorkingDirectory>{escape(str(script.parents[1]))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_wake_task() -> bool:
    """Register (or refresh) the wake-up task. False when Windows refused it."""
    if os.name != "nt":
        return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    xml = PAPER / "wake_task.xml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    xml.write_text(wake_task_xml(pythonw, Path(__file__).resolve()), encoding="utf-16")
    try:
        r = subprocess.run(["schtasks", "/Create", "/TN", WAKE_TASK, "/XML", str(xml), "/F"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        xml.unlink(missing_ok=True)


def uninstall_wake_task() -> None:
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/TN", WAKE_TASK, "/F"], capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-startup", action="store_true")
    ap.add_argument("--uninstall-startup", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--open", action="store_true", help="Mintory 창 열기")
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.install_startup:
        print(f"윈도우 시작 시 자동 실행 등록: {install_startup()}")
        if install_wake_task():
            print(f"평일 23:40에 PC를 깨우는 작업 등록: {WAKE_TASK}")
    elif args.uninstall_startup:
        startup_file().unlink(missing_ok=True)
        uninstall_wake_task()
        print("자동 시작을 해제했습니다.")
    elif args.open:
        sys.exit(open_app())
    elif args.status:
        state = read_state()
        alive = agent_alive()
        if not alive:
            print("자동 실행기: 꺼져 있음 (Mintory를 열면 켜집니다)")
        elif state.get("agent") == "trading":
            print(f"자동 실행기: 지금 모의투자 실행 중 ({state.get('checked_at')} 시작)")
        else:
            print("자동 실행기: 켜져 있음")
        if STOP.exists():
            print("일시정지 중: 주문을 내지 않습니다")
        nxt = next_run(datetime.now(KST), state.get("last_day")).isoformat(timespec="minutes")
        print(f"다음 자동 실행: {nxt}")
        print(f"마지막 실행: {state.get('last_run') or '아직 없음'} ({state.get('last_result') or '-'})")
        if state.get("missed"):
            print("놓친 실행: " + ", ".join(state["missed"]))
    else:
        try:
            loop(restarted=args.restarted)
        except Exception:
            import traceback
            log("자동 실행기 오류로 종료\n" + traceback.format_exc())
            raise


if __name__ == "__main__":
    main()
