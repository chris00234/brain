# Brain recall failure — Korean name lookup

Date: 2026-04-28
Agent: Jenna

## What happened
- Chris asked whether his Korean name was stored in Brain.
- `brain_recall` with query terms like `Chris Korean name Daehyun Korean legal name Hangul` failed to surface the correct personal fact.
- When Jenna attempted to store `Chris's Korean name is Daehyun Cho, written in Hangul as 조대현`, Brain returned `NOOP duplicate`, implying the fact already existed.

## Why this matters
- Recall missed an existing high-value personal fact.
- Store dedupe recognized the duplicate, so recall and dedupe behavior diverged.
- Operationally, a stored fact that recall cannot retrieve is a Brain search-quality failure.

## Jenna rule going forward
For high-value personal facts (legal name, document location, biometrics, immigration case info), one failed recall is insufficient. Retry alternate spellings/collections and verify dedupe/atoms if available before escalating to Chris or saying the value is missing.

## Desired Brain improvement
Investigate why recall did not retrieve the Korean-name atom while dedupe recognized it. Add eval case: query `Chris Korean name Daehyun Hangul 조대현` should retrieve the stored fact.
