#!/usr/bin/env python3
"""
Email Daily Digest — runs once daily (7 PM PST via cron).
Fetches today's emails, classifies them, outputs a summary for Jenna to send to Chris.
No LLM tokens used — pure local IMAP + keyword classification.
"""

import email
import imaplib
import os
import re
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

ENV_FILE = Path(os.getenv("EMAIL_MONITOR_ENV_FILE", str(Path(__file__).with_name(".email_monitor.env"))))


def load_env_file(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(ENV_FILE)

IMAP_HOST = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.getenv("EMAIL_IMAP_USER", "wheogus98@gmail.com")
IMAP_PASS = os.getenv("EMAIL_IMAP_PASS")

if not IMAP_PASS:
    raise RuntimeError("Missing EMAIL_IMAP_PASS")

# --- Classification rules ---

URGENT_SUBJECT = [
    "payment failed",
    "fraud",
    "security alert",
    "account locked",
    "action required",
    "verify your",
    "suspicious",
    "unauthorized",
    "uscis",
    "immigration",
    "i-797",
    "i-751",
    "i-20",
    "appointment confirmed",
    "appointment rescheduled",
    "appointment cancelled",
]

URGENT_FROM = [
    "uscis",
    "@gatech.edu",
    "@cc.gatech.edu",
    "chase.com",
    "bankofamerica",
    "americanexpress",
    "capitalone",
    "wellsfargo",
    "citi.com",
]

IMPORTANT_SUBJECT = [
    "omscs",
    "georgia tech",
    "gatech",
    "deadline",
    "interview",
    "renewal",
    "visa",
    "opt",
    "statement",
    "bill",
    "due",
    "confirmation",
    "reservation",
    "booking",
    "receipt",
    "invoice",
    "scheduled",
    "reminder",
]

IMPORTANT_FROM = [
    "jennacho97@gmail.com",
    "schoolsfirstfcu",
    "vanguard",
    "fidelity",
    "schwab",
    "hawaiianairlines",
    "delta.com",
    "united.com",
    "southwest.com",
    "apple.com",
    "google.com",
]

NOISE_SUBJECT = [
    "newsletter",
    "unsubscribe",
    "promotion",
    "promotional",
    "promo",
    "sale",
    "deal",
    "daily digest",
    "social",
    "digest",
    "new follower",
    "marketing",
    "offer",
    "coupon",
    "sponsored",
    "weekly roundup",
    "weekly update",
    "product update",
    "new features",
    "black friday",
    "cyber monday",
    "limited time",
    "save",
    "discount",
    "% off",
]

NOISE_FROM = [
    "no-reply@",
    "noreply@",
    "news@",
    "newsletter@",
    "updates@",
    "marketing@",
    "promo@",
    "deals@",
    "hello@g.hellofresh",
    "insiderdeals@",
    "extracare@",
    "mail.cb2.com",
    "robinhood.com",
    "linkedin.com",
    "honey.com",
    "pandora.net",
    "beehiiv.com",
]


def decode_mime(value: str) -> str:
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out).strip()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def classify(sender: str, subject: str) -> str:
    """Returns: 'urgent', 'important', 'normal', or 'noise'"""
    s_from = norm(sender)
    s_subj = norm(subject)

    # Noise first
    if any(k in s_subj for k in NOISE_SUBJECT):
        return "noise"
    if any(k in s_from for k in NOISE_FROM):
        return "noise"

    # Urgent
    if any(k in s_subj for k in URGENT_SUBJECT):
        return "urgent"
    if any(k in s_from for k in URGENT_FROM):
        return "urgent"

    # Important
    if any(k in s_subj for k in IMPORTANT_SUBJECT):
        return "important"
    if any(k in s_from for k in IMPORTANT_FROM):
        return "important"

    return "normal"


def parse_date_safe(date_str: str):
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return None


def main():
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    M.login(IMAP_USER, IMAP_PASS.replace(" ", ""))
    M.select("INBOX", readonly=True)

    # Search for today's emails (SINCE today)
    today = datetime.now(UTC).strftime("%d-%b-%Y")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f"SINCE {yesterday}")

    if typ != "OK" or not data or not data[0]:
        M.logout()
        print("NO_NEW_EMAILS")
        return

    ids = data[0].split()
    results = {"urgent": [], "important": [], "normal": [], "noise_count": 0}

    for uid in ids:
        typ, msg_data = M.fetch(uid, "(RFC822.HEADER)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        subject = decode_mime(msg.get("Subject", ""))
        sender = decode_mime(msg.get("From", ""))
        date_str = msg.get("Date", "")

        category = classify(sender, subject)

        entry = {
            "from": sender,
            "subject": subject,
            "date": date_str,
        }

        if category == "urgent":
            results["urgent"].append(entry)
        elif category == "important":
            results["important"].append(entry)
        elif category == "normal":
            results["normal"].append(entry)
        else:
            results["noise_count"] += 1

    M.logout()

    # Output summary
    total = (
        len(results["urgent"]) + len(results["important"]) + len(results["normal"]) + results["noise_count"]
    )

    print(f"📧 오늘의 이메일 요약 ({total}통)")
    print()

    if results["urgent"]:
        print(f"🚨 긴급 ({len(results['urgent'])})")
        for e in results["urgent"]:
            print(f"  • {e['subject']}")
            print(f"    From: {e['from']}")
        print()

    if results["important"]:
        print(f"📌 중요 ({len(results['important'])})")
        for e in results["important"]:
            print(f"  • {e['subject']}")
            print(f"    From: {e['from']}")
        print()

    if results["normal"]:
        print(f"📋 일반 ({len(results['normal'])})")
        for e in results["normal"][:5]:
            print(f"  • {e['subject']}")
        if len(results["normal"]) > 5:
            print(f"  ... +{len(results['normal']) - 5}개")
        print()

    if results["noise_count"]:
        print(f"🗑️ 무시 가능: {results['noise_count']}통")

    if not results["urgent"] and not results["important"] and not results["normal"]:
        print("특별한 메일 없음 ✅")


if __name__ == "__main__":
    main()
