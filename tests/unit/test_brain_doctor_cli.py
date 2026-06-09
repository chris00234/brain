from __future__ import annotations

import importlib.util
import json
import urllib.request
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

BRAIN_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("brain_doctor", BRAIN_ROOT / "cli/brain_doctor.py")
brain_doctor = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(brain_doctor)


class _Resp:
    def __init__(self, payload: dict):
        self.payload = payload
        self.headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_recall_diagnostic_is_compact_and_read_only(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=10):
        seen.append((req.full_url, timeout, req.get_method()))
        return _Resp(
            {
                "query": "한국어 주소",
                "count": 2,
                "results": [
                    {
                        "id": "a1",
                        "title": "Chris address",
                        "collection": "canonical",
                        "score": 0.9,
                        "confidence": 0.8,
                        "content": "Chris address answer\nwith private trailing details" + ("x" * 500),
                        "metadata": {"large": "y" * 1000},
                    }
                ],
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(brain_doctor, "_bearer", lambda: "token")

    out = brain_doctor._recall_diagnostic("한국어 주소", limit=1)

    assert out["diagnostic"] == "brain_doctor_recall_v1"
    assert out["safe"] is True
    assert out["side_effects"].startswith("none")
    assert out["returned"] == 1
    assert "metadata" not in out["results"][0]
    assert len(out["results"][0]["content_preview"]) == 240
    assert "/recall/v2?" in seen[0][0]
    assert seen[0][2] == "GET"


def test_recall_diagnostic_reports_raw_http_401_without_secret(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise HTTPError(req.full_url, 401, "Unauthorized", hdrs=Message(), fp=BytesIO(b"nope"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(brain_doctor, "_bearer", lambda: "super-secret-token")

    out = brain_doctor._recall_diagnostic("auth check", limit=1)

    assert out["diagnostic"] == "brain_doctor_recall_v1"
    assert out["safe"] is True
    assert out["results"] == []
    assert "HTTP 401" in out["error"]
    rendered = json.dumps(out)
    assert "super-secret-token" not in rendered
    assert "mcp_credential_path" in rendered
