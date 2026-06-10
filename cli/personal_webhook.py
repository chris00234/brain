#!/opt/homebrew/bin/python3
"""Personal webhook — receives iOS Shortcuts POSTs for location and health.

Replacement for the original ~/.openclaw/workspace-jenna/scripts/personal_webhook.py
(deleted during OpenClaw → Hermes migration, 2026-05-23). Re-created from the
nginx contract at ~/server/nginx/conf.d/personal-webhook.conf.

Listens on 127.0.0.1:8790. Public hostnames `location.chrischodev.com` and
`health.chrischodev.com` reach this via nginx → host.docker.internal:8790.

Routes:
  GET  /healthz                    → "ok" plus tiny JSON status
  POST /location, /location/ingest → write incoming JSON to brain logs +
                                     POST to brain /memory (kind=location_ping)
  POST /health,   /health/ingest   → same shape with kind=health_ping
  *                                → 404

Auth: Bearer token at `~/.brain/credentials/.personal_webhook_secret`. Compares
exact string. (Cloudflare Access is the primary edge auth; bearer is the
fallback the webhook validates itself.)

Persistence:
  ~/server/brain/logs/personal-webhook.log              (line per request)
  ~/server/brain/logs/personal-webhook-failures.jsonl   (auth failures)

Stdlib only. Runs as launchd agent `ai.brain.personal-webhook`.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

HOST = "127.0.0.1"
PORT = 8790
SECRET_FILE = Path.home() / ".brain/credentials/.personal_webhook_secret"
LOG_DIR = Path.home() / "server/brain/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "personal-webhook.log"
FAIL_LOG = LOG_DIR / "personal-webhook-failures.jsonl"
BRAIN_URL = "http://127.0.0.1:8791/memory"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("personal_webhook")
log.addHandler(logging.StreamHandler(sys.stdout))


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _load_secret() -> str | None:
    try:
        return SECRET_FILE.read_text().strip()
    except OSError:
        return None


def _record_failure(peer: str, path: str, reason: str) -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000000"),
        "peer": peer,
        "path": path,
        "reason": reason,
    }
    try:
        with FAIL_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _post_to_brain(payload: dict, kind: str, secret: str) -> None:
    """Best-effort POST to brain /memory. Non-blocking semantics — log + drop."""
    body = {
        "content": f"[{kind}] " + json.dumps(payload)[:1800],
        "kind": kind,
        "tags": ["source:ios_shortcut", f"kind:{kind}", "agent:personal_webhook"],
        "confidence": 0.6,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BRAIN_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:  # pragma: no cover
        log.warning("brain ingest %s failed: %s", kind, exc)


# ─── HTTP handler ────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "personal-webhook/1.0"

    # quieter access log (we keep our own via logger)
    def log_message(self, fmt: str, *args: object) -> None:
        log.info("[%s] " + fmt, self.address_string(), *args)

    def _peer(self) -> str:
        # X-Forwarded-For preferred (nginx sets it). Fallback to socket peer.
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def _auth_ok(self) -> tuple[bool, str]:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False, "missing or malformed Authorization header"
        provided = header.split(" ", 1)[1].strip()
        secret = _load_secret()
        if not secret:
            return False, "server secret unreadable"
        # Constant-time compare not strictly needed for personal endpoint, but cheap
        import hmac

        if not hmac.compare_digest(provided, secret):
            return False, "bearer token mismatch"
        return True, ""

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "service": "personal_webhook", "ts": int(time.time())})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        # Map routes → kind tag
        route_map = {
            "/location": "location_ping",
            "/location/ingest": "location_ping",
            "/health": "health_ping",
            "/health/ingest": "health_ping",
        }
        kind = route_map.get(path)
        if kind is None:
            self._send_json(404, {"error": "unknown route"})
            return

        ok, reason = self._auth_ok()
        if not ok:
            _record_failure(self._peer(), path, reason)
            self._send_json(401, {"error": reason})
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:  # nginx already enforces this; double-check
            self._send_json(413, {"error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        secret = _load_secret() or ""
        _post_to_brain(payload, kind, secret)
        log.info("ingested kind=%s bytes=%d peer=%s", kind, length, self._peer())
        self._send_json(200, {"status": "ingested", "kind": kind})


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    if not SECRET_FILE.is_file():
        log.error("secret file missing at %s — refusing to start", SECRET_FILE)
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("personal_webhook listening on %s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("personal_webhook shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
