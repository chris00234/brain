"""brain_core/_watchdog.py — SIGALRM wall-clock watchdog helper.

Pattern established by ``episode_binder.py`` after the 7GB RSS leak
incident: a hung Qdrant scroll + scheduler subprocess reaper (1h cap)
still let a single job accumulate multi-GB RSS before the reaper fired.
Every heavy pipeline that issues unbounded Qdrant/Ollama/LLM calls
arms a wall-clock alarm at startup so a stuck RPC dies deterministically.

POSIX-only (SIGALRM); this module is safe to import on macOS/Linux.
"""

from __future__ import annotations

import signal
import sys
from datetime import UTC, datetime
from types import FrameType


def arm(seconds: int, *, tag: str) -> None:
    """Kill the process with exit 124 if ``seconds`` elapses.

    ``tag`` is printed on timeout so scheduler logs identify the
    offending pipeline without stack-trace digging.
    """

    def _timeout(signum: int, frame: FrameType | None) -> None:
        print(
            f"[{tag}] FATAL: exceeded {seconds}s wall-clock at " f"{datetime.now(UTC).isoformat()}, aborting",
            flush=True,
        )
        sys.exit(124)

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(seconds)
