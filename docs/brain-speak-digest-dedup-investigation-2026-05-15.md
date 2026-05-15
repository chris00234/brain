# brain_speak_digest dedup investigation — 2026-05-15

## Trigger

Sage reported the same `synthesis_drive/pattern` observation appearing three times in ~28 minutes:

> Brain work is shifting from feature delivery into integration debt: many pending core edits need one clean verification pass.

## Findings

- `brain_speak_digest` itself ran once at `2026-05-15T07:55:00` local time (`scheduler_history.db`, job id `67802`).
- The observation was emitted once into `brain_speak_log` at `2026-05-15T14:55:09+00:00` with:
  - `drive=synthesis_drive`
  - `category=pattern`
  - `severity=6.0`
  - `sent_via=self_handled`
  - `dedup_key=synth:ffe9096a5867`
- `was_sent_recently('synth:ffe9096a5867', within_h=24)` returns `True`; the digest composer would suppress the same observation on another digest run.
- The three repeats came from the task-dispatch path, not from repeated digest emission:
  - one handoff task was created: `task_229aab59cca2`
  - dispatch attempts retried the same task three times after OpenClaw timeouts:
    - `14:55:45Z → 14:59:29Z`, `timeout after 100s`
    - `15:09:57Z → 15:13:37Z`, `timeout after 100s`
    - `15:23:57Z → 15:27:37Z`, `timeout after 100s`

## Current dedup behavior

- Observation-level dedup exists in `speak_schema.was_sent_recently()` and is applied by `speak_composer.compose_digest()` before routing.
- The default observation dedup window is 72h (`DEDUP_WINDOW_H = 72`).
- Severity 6.0 does not bypass this gate.
- Command proposals have an extra 168h check in `speak_synthesis._emit_commands()`.

## Root cause

The digest observation is converted into a handoff task by `agent_messenger._create_handoff_task()`. Once it becomes a task, retries are governed by `task_queue.process_ready()` / `defer_task()` rather than the speak dedup gate. Transient dispatch failures re-approve the same task with a 600s retry window and no max-attempt cap for proactive digest handoffs, so the same observation can be re-sent to the target agent repeatedly while the backend is timing out.

## Proposed fix

Add a proactive-handoff retry guard in the task-dispatch layer, not in `speak_synthesis`:

1. For tasks with `metadata.source == 'brain_speak_digest'`, `metadata.message_type == 'handoff'`, and a stable `dedup_key`, cap transient dispatch retries to 1 attempt per 24h per dedup key.
2. On a transient timeout after that cap, mark the task `paused` or `deferred` with `next_attempt_at = now + 24h` instead of re-approving it after 600s.
3. Optionally store `proactive_retry_suppressed_at` and `proactive_retry_suppressed_reason` in task metadata for audit visibility.
4. Add a unit test that creates a digest handoff task with a `dedup_key`, records a transient dispatch failure, and verifies it does not become ready again inside 24h.

## Confirmation

The exact observation will not re-trigger through `brain_speak_digest` without state change during the current dedup window because `brain_speak_log` already contains `synth:ffe9096a5867` and `was_sent_recently()` returns true. The remaining duplicate surface is task retry replay after dispatch timeout.
