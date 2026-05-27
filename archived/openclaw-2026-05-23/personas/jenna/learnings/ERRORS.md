2026-04-14 — AppleScript Calendar query hung under exec-event heartbeat
- What happened: `osascript` Calendar query for events in the next 30 minutes started as session `sharp-mist` and exited by SIGTERM with no output.
- What to do differently: use a bounded timeout/fallback for Calendar access during exec-event/heartbeat flows, and prefer a non-GUI-safe path or fail fast instead of waiting on Calendar.app automation.

2026-04-14 — Background python heredoc was SIGTERM'd with no output
- What happened: async exec session `nova-willow` (`python3 <<'PY'`) exited with signal SIGTERM and recorded no stdout/stderr, so the underlying step was not inspectable after the fact.
- What to do differently: when launching background Python heredocs, persist the exact script or emit an initial identifier/log line immediately so postmortems can recover what was running before a SIGTERM.

2026-04-14 — Calendar AppleEvent timed out during async exec
- What happened: async exec session `tidy-gla` finished with AppleScript error `Calendar got an error: AppleEvent timed out. (-1712)`.
- What to do differently: avoid relying on Calendar.app AppleEvents in unattended/background flows, or wrap them in a shorter bounded timeout with a fallback path so they fail fast and visibly.

2026-04-15 — Async exec session `nova-orb` was SIGTERM'd with no postmortem detail
- What happened: an earlier async command completed with `Exec failed (nova-orb, signal SIGTERM)` and no stdout/stderr payload, so there was no recoverable context about what step was interrupted.
- What to do differently: background jobs should always emit an early identifying log line and, when practical, write to a dedicated logfile so a later SIGTERM still leaves enough context for recovery.
