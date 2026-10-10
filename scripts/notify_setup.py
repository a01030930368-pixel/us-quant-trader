"""텔레그램 알림 연결 도우미. notify_setup.bat 이 실행한다.

    python scripts/notify_setup.py          # 봇 토큰을 받아 연결하고 시험 메시지 보내기
    python scripts/notify_setup.py --test   # 이미 연결된 봇으로 시험 메시지만 보내기

토큰과 대화방 번호는 .env 의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 에 저장한다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quant import notify  # noqa: E402

STEPS = """
Mintory 텔레그램 알림 연결

  1. 휴대폰에 텔레그램 앱을 설치하고 가입합니다.
  2. 텔레그램 검색창에 BotFather 를 검색해서 파란 체크 표시가 있는 것을 엽니다.
  3. 시작(Start)을 누르고 /newbot 을 보냅니다.
  4. 봇 이름을 물어보면: Mintory
  5. 봇 아이디를 물어보면 bot 으로 끝나는 아무 이름 (예: mintory_hyunmin_bot)
  6. BotFather 가 보내준 긴 토큰(숫자:영문 섞인 글자)을 복사합니다.
     PC 텔레그램이 없으면 휴대폰에서 복사해 카톡 나에게 보내기 등으로 PC로 옮기세요.
"""


def set_env(values: dict[str, str], path: Path = notify.ENV) -> None:
    """Replace or append KEY=value lines in .env, keeping everything else."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bot_name(token: str) -> str | None:
    try:
        r = requests.get(f"{notify.API}/bot{token}/getMe", timeout=15)
        return r.json()["result"]["username"] if r.ok else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def wait_for_chat(token: str, seconds: int = 180) -> str | None:
    """Wait until someone messages the bot and return that chat's id."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{notify.API}/bot{token}/getUpdates", params={"timeout": 20}, timeout=30)
            for u in reversed(r.json().get("result", [])):
                chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
                if chat:
                    return str(chat["id"])
        except (requests.RequestException, ValueError):
            time.sleep(3)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="시험 메시지만 보내기")
    args = ap.parse_args()

    if args.test:
        ok = notify.send("Mintory 시험 알림이에요. 잘 보이면 연결 완료! ✅")
        print("시험 알림을 보냈어요. 휴대폰을 확인하세요." if ok else
              "보내지 못했어요. notify_setup.bat 으로 다시 연결해 주세요.")
        return 0 if ok else 1

    print(STEPS)
    token = input("복사한 토큰을 붙여넣고 Enter (마우스 오른쪽 클릭 = 붙여넣기): ").strip()
    name = bot_name(token)
    if not name:
        print("\n토큰이 맞지 않아요. BotFather 가 보낸 토큰 전체를 다시 복사해서 시도해 주세요.")
        return 1
    print(f"\n봇 확인: @{name}")
    print(f"이제 텔레그램에서 @{name} 을 검색해서 열고, 시작(Start)을 누르거나 아무 말이나 보내세요.")
    print("기다리는 중... (3분)")
    chat_id = wait_for_chat(token)
    if not chat_id:
        print("\n봇에게 온 메시지를 찾지 못했어요. 봇에게 메시지를 보낸 뒤 다시 실행해 주세요.")
        return 1
    set_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    if notify.send("Mintory 알림이 연결됐어요 ✅\n주문이 나가거나 문제가 생기면, 그리고 매일 밤 실행이 끝나면 여기로 알려드릴게요."):
        print("\n연결 완료! 휴대폰에 시험 메시지가 왔는지 확인하세요.")
        print(".env 에 저장했어요. 매일 밤 요약을 끄려면 .env 에 NOTIFY_DAILY=0 을 추가하세요.")
        return 0
    print("\n저장은 했지만 시험 메시지를 보내지 못했어요. 인터넷 연결을 확인하고 notify_setup.bat 을 다시 실행해 주세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
