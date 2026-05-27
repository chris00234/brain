#!/opt/homebrew/bin/python3
"""Daily reflection — sends Chris one introspective question per evening via Telegram.

Question rotates by day of week. Response is captured by Jenna in normal conversation
flow and stored to RAG via the feedback capture protocol.
"""

import subprocess
from datetime import datetime
from pathlib import Path

# 7-day rotation, mix of English/Korean — used as fallback when daily_synthesis.py
# hasn't produced a data-driven question for tonight.
QUESTIONS = {
    0: "🌙 Monday reflection — What was the best thing you built or learned today?",  # Mon
    1: "🌙 Tuesday — 지금 가장 신경 쓰이는 일이 뭐야? (What's currently weighing on you?)",  # Tue
    2: "🌙 Wednesday — What did you change your mind about recently?",  # Wed
    3: "🌙 Thursday — 이번 주에 누가 너의 에너지를 가장 많이 가져갔어? (Who took the most energy this week?)",  # Thu
    4: "🌙 Friday — What's one thing from this week you want to remember 6 months from now?",  # Fri
    5: "🌙 Saturday — 이번 주에 피한 일이 있어? 왜 피했는지도 같이. (What did you avoid this week, and why?)",  # Sat
    6: "🌙 Sunday — What's the main goal for next week? Just one.",  # Sun
}

CHAT_ID = "8484060831"
HERMES_BIN = "/Users/chrischo/.local/bin/hermes"
TONIGHT_REFLECTION = Path("/Users/chrischo/.hermes/profiles/jenna/.tonight_reflection.txt")


def get_question():
    # Prefer the data-driven question Jenna's daily_synthesis.py wrote at 21:00.
    # Falls back to the static rotation if synthesis didn't run or had nothing to ask.
    try:
        if TONIGHT_REFLECTION.exists():
            content = TONIGHT_REFLECTION.read_text().strip()
            if content:
                return f"🌙 {content}"
    except Exception:
        pass
    weekday = datetime.now().weekday()  # Monday=0
    return QUESTIONS[weekday]


def send_telegram(text):
    cmd = [
        HERMES_BIN,
        "send",
        "--to",
        f"telegram:{CHAT_ID}",
        text,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
    return result.returncode == 0


def main():
    question = get_question()
    print(f"[{datetime.now().isoformat()}] Sending: {question}")
    if send_telegram(question):
        print("✅ Sent")
    else:
        print("❌ Failed to send")


if __name__ == "__main__":
    main()
