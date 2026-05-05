#!/opt/homebrew/bin/python3
"""Gmail intelligent ingest — Jenna classifies surviving emails before storing.

Stage 1: IMAP pull of new emails since last UID.
Stage 2: Heuristic pre-filter — drop ~90% of inbox volume (newsletters, OTPs,
         autoresponders, list mail, marketing) BEFORE any LLM dispatch.
Stage 3: Batch dispatch surviving candidates to Jenna agent for keep/skip
         classification + 1-sentence summary + signal score (0-10).
Stage 4: Write only `keep=true && signal_score >= 6` emails as schema-compliant
         raw records, storing Jenna's summary as the searchable content,
         NOT the raw HTML body.

Reuses the same .email_monitor.env file as email_important_monitor.py for
IMAP credentials. Owned by Jenna.

Usage:
  ingest_gmail.py [--dry-run] [--lookback N] [--days-back N]
"""

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from pathlib import Path
from llm_dispatch import dispatch_json
import logging

log = logging.getLogger("brain.gmail")


# ── Config ──────────────────────────────────────────────
ENV_FILE = Path.home() / ".openclaw/credentials/gmail-imap.env"
STATE_FILE = Path("/Users/chrischo/.openclaw/workspace-jenna/.gmail_ingest_state.json")
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-jenna/logs/gmail-ingest-failures.jsonl")

AGENT = "jenna"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from inbox_utils import is_near_duplicate as _is_near_duplicate_shared


def _is_near_duplicate(content: str, inbox_dir: Path, window: int = 50, threshold: float = 0.7) -> bool:
    return _is_near_duplicate_shared(content, window=window, threshold=threshold, inbox_dir=inbox_dir)


DISPATCH_TIMEOUT = 240
SIGNAL_THRESHOLD = 6
BATCH_SIZE = 50
MAX_BODY_CHARS = 1500  # cap body sent to Jenna; she only needs subject + first chunk


def load_env_file(path: Path) -> None:
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


# ── Heuristic pre-filter ────────────────────────────────
NOISE_SENDER_PATTERNS = [
    re.compile(r"no[-_]?reply@", re.I),
    re.compile(r"noreply@", re.I),
    re.compile(r"@news(letter)?\.", re.I),
    re.compile(r"@updates?\.", re.I),
    re.compile(r"@marketing\.", re.I),
    re.compile(r"@notifications?\.", re.I),
    re.compile(r"^bounces?@", re.I),
    re.compile(r"@discord\.com$", re.I),
    re.compile(r"@reddit\.com$", re.I),
    re.compile(r"@medium\.com$", re.I),
]

NOISE_SUBJECT_PATTERNS = [
    re.compile(r"\b(unsubscribe|opt[\s-]out)\b", re.I),
    re.compile(r"\b(otp|verification[\s-]?code|one[\s-]?time[\s-]?code)\b", re.I),
    re.compile(r"\b(your\s+(weekly|daily|monthly)\s+(digest|update|recap|roundup))\b", re.I),
    re.compile(r"\b(\d+%\s*off|sale|discount|promo|coupon|deal)\b", re.I),
    re.compile(r"\b(black\s+friday|cyber\s+monday|limited\s+time)\b", re.I),
    re.compile(r"\b(new\s+follower|liked\s+your|commented\s+on)\b", re.I),
]


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception as exc:
        log.debug("gmail: failure-log write skipped: %s", exc)


try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load
    from safe_state import save_state as _safe_save

    def load_state() -> dict:
        state = _safe_load(STATE_FILE)
        return state if state else {"last_uid": 0}

    def save_state(state: dict) -> None:
        _safe_save(STATE_FILE, state)
except ImportError:

    def load_state() -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {"last_uid": 0}
        return {"last_uid": 0}

    def save_state(state: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))


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


def extract_body(msg) -> str:
    """Walk the multipart MIME tree, extract the first text/plain (or text/html stripped) body."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                try:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="ignore"
                    )
                    break
                except Exception:
                    continue
            elif ctype == "text/html" and not text:
                try:
                    raw = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="ignore"
                    )
                    text = re.sub(r"<[^>]+>", " ", raw)
                except Exception:
                    continue
    else:
        try:
            text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            text = msg.get_payload(decode=False) or ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_BODY_CHARS]


def passes_prefilter(sender: str, subject: str, list_unsubscribe: str, list_id: str, body_len: int) -> bool:
    """Drop ~90% of inbox before LLM ever sees it."""
    if list_unsubscribe.strip():
        return False  # Marketing/newsletter
    if list_id.strip():
        return False  # List mail
    if body_len < 100:
        return False  # Auto-notifications, OTPs
    s_from = sender.lower()
    s_subj = subject.lower()
    if any(p.search(s_from) for p in NOISE_SENDER_PATTERNS):
        return False
    if any(p.search(s_subj) for p in NOISE_SUBJECT_PATTERNS):
        return False
    return True


def fetch_candidates(days_back: int, lookback: int) -> tuple[list[dict], int]:
    if not IMAP_PASS:
        log_failure("EMAIL_IMAP_PASS not set in .email_monitor.env")
        sys.stderr.write("DISPATCH_FAIL adapter=gmail reason=missing IMAP password\n")
        return [], 0

    state = load_state()
    last_uid = state.get("last_uid", 0)

    M = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
    try:
        M.login(IMAP_USER, IMAP_PASS)
        M.select("INBOX")

        since_str = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        typ, data = M.uid("search", None, f'(SINCE "{since_str}")')
        if typ != "OK" or not data or not data[0]:
            return [], last_uid

        ids = data[0].split()
        if lookback:
            ids = ids[-lookback:]

        candidates = []
        new_max_uid = last_uid
        for uid in ids:
            try:
                uid_int = int(uid.decode())
            except ValueError:
                continue
            if uid_int <= last_uid:
                continue
            new_max_uid = max(new_max_uid, uid_int)

            typ, msg_data = M.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_mime(msg.get("Subject", ""))
            sender = decode_mime(msg.get("From", ""))
            date_hdr = decode_mime(msg.get("Date", ""))
            list_unsubscribe = msg.get("List-Unsubscribe", "")
            list_id = msg.get("List-Id", "")
            body = extract_body(msg)

            if not passes_prefilter(sender, subject, list_unsubscribe, list_id, len(body)):
                continue

            candidates.append(
                {
                    "uid": uid_int,
                    "subject": subject,
                    "sender": sender,
                    "date": date_hdr,
                    "body": body,
                }
            )

        return candidates, new_max_uid
    finally:
        try:
            M.logout()
        except Exception:
            pass


def build_classification_prompt(batch: list[dict]) -> str:
    lines = []
    lines.append("You are Jenna, Chris's chief of staff. Classify these emails.")
    lines.append("")
    lines.append("Goal: keep ONLY emails worth remembering 6 months from now. Drop newsletters,")
    lines.append("automated notifications, marketing, social media noise. Keep personal correspondence,")
    lines.append("financial/legal/medical, travel confirmations, deadlines, real conversations.")
    lines.append("")
    for i, e in enumerate(batch, 1):
        lines.append(f"--- EMAIL {i} (uid={e['uid']}) ---")
        lines.append(f"From: {e['sender']}")
        lines.append(f"Subject: {e['subject']}")
        lines.append(f"Date: {e['date']}")
        lines.append(f"Body: {e['body'][:600]}")
        lines.append("")
    lines.append("=" * 60)
    lines.append("OUTPUT FORMAT (return ONLY valid JSON, no markdown fences):")
    lines.append('{"classifications": [')
    lines.append(
        '  {"uid": <int>, "keep": <bool>, "category": "personal|work|financial|legal|medical|travel|deadline|other|noise", "summary": "<1 sentence>", "signal_score": <0-10>}'
    )
    lines.append("]}")
    lines.append("")
    lines.append("STRICT: only the JSON object. Empty list allowed if all are noise.")
    return "\n".join(lines)


def dispatch_classification(prompt: str) -> dict | None:
    result = dispatch_json(
        agent=AGENT,
        prompt=prompt,
        timeout=DISPATCH_TIMEOUT,
        log_failure=log_failure,
        source="ingest.gmail",
        thinking="low",
    )
    if result is None:
        sys.stderr.write(f"DISPATCH_FAIL agent={AGENT} backend=cli_llm\n")
    return result


def write_kept_email(email_data: dict, classification: dict) -> Path | None:
    text = (
        f"From: {email_data['sender']}\n"
        f"Subject: {email_data['subject']}\n"
        f"Date: {email_data['date']}\n"
        f"Category: {classification.get('category', 'other')}\n"
        f"Signal: {classification.get('signal_score', 0)}/10\n"
        f"\n"
        f"Summary (Jenna): {classification.get('summary', '')}\n"
        f"\n"
        f"Body excerpt:\n{email_data['body'][:600]}"
    )
    digest = hashlib.sha256(f"{email_data['uid']}:{text}".encode()).hexdigest()
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    date_part = now[:10].replace("-", "_")
    rec_id = f"raw_gmail_{date_part}_{digest[:8]}"
    record = {
        "id": rec_id,
        "timestamp": now,
        "source_type": "gmail",
        "source_ref": f"gmail:uid:{email_data['uid']}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": text,
        "attachments": [],
        "entities": ["Chris", classification.get("category", "other")],
        "hash": f"sha256:{digest}",
    }
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f"{rec_id}.json"
    if out.exists():
        return None
    # Cross-source semantic dedup — check if near-identical content already in inbox
    if _is_near_duplicate(text, INBOX_DIR):
        return None
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail intelligent ingest — Jenna classifies survivors")
    parser.add_argument("--days-back", type=int, default=2, help="IMAP search lookback in days")
    parser.add_argument("--lookback", type=int, default=200, help="Max recent UIDs to consider")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print pre-filter survivors without dispatching"
    )
    args = parser.parse_args()

    print(f"Gmail ingest — IMAP {IMAP_HOST}, days_back={args.days_back}")

    print("[1/4] IMAP pull + heuristic pre-filter...")
    # 2026-04-18: load state ONCE at entry and keep it through the run.
    # Previous code called load_state() twice (line ~381 + ~434) which, if a
    # concurrent gmail_ingest run advanced last_uid in between, would rewind
    # the watermark with the older local value and cause duplicate processing
    # of already-seen emails. Single load, then guarded write.
    state = load_state()
    candidates, new_max_uid = fetch_candidates(args.days_back, args.lookback)
    print(f"  {len(candidates)} candidates after pre-filter")
    if not candidates:
        if new_max_uid > state.get("last_uid", 0):
            state["last_uid"] = new_max_uid
            save_state(state)
        print("Nothing to classify.")
        return

    if args.dry_run:
        print("\n[DRY RUN] survivors:")
        for c in candidates[:10]:
            print(f"  uid={c['uid']}  from={c['sender'][:40]}  subj={c['subject'][:60]}")
        if len(candidates) > 10:
            print(f"  ... ({len(candidates) - 10} more)")
        print(f"\n[DRY RUN] would dispatch {len(candidates)} to Jenna in batches of {BATCH_SIZE}")
        return

    print("[2/4] Dispatching to Jenna in batches...")
    all_classifications: list[dict] = []
    any_batch_failed = False
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i : i + BATCH_SIZE]
        prompt = build_classification_prompt(batch)
        result = dispatch_classification(prompt)
        if result is None:
            import time

            time.sleep(10)
            result = dispatch_classification(prompt)
        if not result:
            print(f"  Batch {i // BATCH_SIZE + 1}: FAILED")
            any_batch_failed = True
            continue
        all_classifications.extend(result.get("classifications", []))
        print(f"  Batch {i // BATCH_SIZE + 1}: {len(result.get('classifications', []))} classified")

    print(f"[3/4] Filtering by signal_score >= {SIGNAL_THRESHOLD}...")
    by_uid = {c["uid"]: c for c in candidates}
    written = 0
    for cls in all_classifications:
        if not cls.get("keep"):
            continue
        if cls.get("signal_score", 0) < SIGNAL_THRESHOLD:
            continue
        uid = cls.get("uid")
        if uid not in by_uid:
            continue
        out = write_kept_email(by_uid[uid], cls)
        if out:
            written += 1

    # Save state AFTER successful dispatch + write — not before.
    # Don't advance watermark if any batch failed (would skip those emails permanently)
    # 2026-04-18: reuse the state dict loaded at entry; avoid second load_state()
    # which could pull a competitor run's higher last_uid and rewind it.
    if not any_batch_failed:
        state["last_uid"] = new_max_uid
        save_state(state)
    else:
        print("  WARNING: watermark NOT advanced due to batch failure(s) — will retry next run")

    print(f"[4/4] Wrote {written} schema-compliant raw records to {INBOX_DIR}")

    if written == 0 and len(candidates) > 0:
        sys.stderr.write(
            f"WARN adapter=gmail candidates={len(candidates)} written=0 — dispatch may have failed\n"
        )


if __name__ == "__main__":
    main()
