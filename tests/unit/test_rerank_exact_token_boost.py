"""Exact-token / code-ish alias boost in rerank.

The current jaccard-based rerank treats every token equally. Code-ish
identifiers (``CODEX_HOME``, ``claude1``, paths like ``/Users/chrischo/.codex``,
Hangul names) deserve more weight: when the query mentions one verbatim and a
candidate result mentions the same string verbatim in title/path/body, that
match is far more likely to be the right answer than a fuzzy-jaccard hit on
some other prose result.

These tests pin the desired behavior. RED before the rerank change ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _score(query: str, result: dict) -> float:
    from rerank import score_result

    return score_result(query, dict(result))


def test_score_result_boosts_exact_env_var_in_path_over_prose():
    """Query ``CODEX_HOME`` must score the result whose path contains the
    literal ``CODEX_HOME`` higher than a prose blurb that mentions only
    ``codex``.
    """
    target = {
        "title": "Codex CLI profiles",
        "content": "Set CODEX_HOME=/Users/chrischo/.codex to point the codex CLI at the local profile dir.",
        "path": "/Users/chrischo/server/knowledge/codex/CODEX.md",
        "score": 40,
        "trust_tier": 1,
    }
    decoy = {
        "title": "Codex notes",
        "content": "We use the codex tool sometimes for second-opinion reviews.",
        "path": "/Users/chrischo/server/brain/docs/codex-notes.md",
        "score": 55,  # higher base vector score, but no exact alias match
        "trust_tier": 1,
    }

    assert _score("CODEX_HOME", target) > _score(
        "CODEX_HOME", decoy
    ), "exact env-var match in title/content/path must beat higher-base-score prose hit"


def test_score_result_boosts_path_alias_match():
    """Query is the literal home-dir path. A doc whose body/path contains the
    same path must outrank a tangential ``codex`` mention.
    """
    target = {
        "title": "Codex CLI profiles",
        "content": "CODEX_HOME=/Users/chrischo/.codex tells codex CLI where to look.",
        "path": "/Users/chrischo/server/knowledge/codex/CODEX.md",
        "score": 40,
        "trust_tier": 1,
    }
    decoy = {
        "title": "Codex random",
        "content": "Codex is a CLI tool.",
        "path": "/notes/random.md",
        "score": 60,
        "trust_tier": 1,
    }

    assert _score("/Users/chrischo/.codex", target) > _score("/Users/chrischo/.codex", decoy)


def test_score_result_boosts_claude_account_aliases():
    """Multi-account aliases like ``claude1 claude2 claude3 claude4`` need to
    survive tokenization AND boost the doc that lists them verbatim.
    """
    target = {
        "title": "Claude Code accounts",
        "content": "Active accounts: claude1 claude2 claude3 claude4. Rotate via OAuth profile.",
        "path": "/Users/chrischo/.claude/accounts.md",
        "score": 40,
        "trust_tier": 1,
    }
    decoy = {
        "title": "Claude usage notes",
        "content": "Claude is Anthropic's assistant model family.",
        "path": "/notes/claude.md",
        "score": 60,
        "trust_tier": 1,
    }

    query = "claude1 claude2 claude3 claude4"
    assert _score(query, target) > _score(query, decoy)


def test_score_result_boosts_hangul_personal_name():
    """Hangul name match must outrank a doc that only mentions Chris's Latin
    name. The tokenizer already keeps Hangul tokens; the exact-token boost
    layers on top so canonical identity wins.
    """
    target = {
        "title": "Chris identity",
        "content": "Name: Chris Cho · Hangul: 조대현 · Email: wheogus98@gmail.com",
        "path": "/Users/chrischo/server/knowledge/canonical/chris/_identity.md",
        "score": 40,
        "trust_tier": 2,
    }
    decoy = {
        "title": "Korean memo",
        "content": "오늘은 좋은 날입니다.",
        "path": "/notes/korean.md",
        "score": 60,
        "trust_tier": 1,
    }

    assert _score("조대현", target) > _score("조대현", decoy)


def test_score_result_boost_is_capped_when_no_exact_alias_in_query():
    """When the query has no code-ish aliases (plain prose), the new boost
    must NOT fire — keep behavior stable for the common case so we don't
    regress existing eval pass rate.
    """
    a = {
        "title": "Random notes",
        "content": "How to ship code on Fridays without anxiety.",
        "path": "/notes/random.md",
        "score": 50,
        "trust_tier": 1,
    }
    score = _score("how to ship code", a)
    # Score must remain in the same order of magnitude as the base — no
    # runaway exact-alias multiplier kicking in.
    assert 0.5 * a["score"] <= score <= 5.0 * a["score"]


# ── Tokenizer keeps exact aliases for the recall_bridge helper ──────────


def test_tokenizer_extract_exact_aliases_keeps_uppercase_env_var():
    from tokenizer import extract_exact_aliases

    aliases = set(extract_exact_aliases("CODEX_HOME and PATH are env vars"))
    assert "CODEX_HOME" in aliases
    assert "PATH" in aliases


def test_tokenizer_extract_exact_aliases_keeps_absolute_path():
    from tokenizer import extract_exact_aliases

    aliases = set(extract_exact_aliases("look in /Users/chrischo/.codex for profiles"))
    assert "/Users/chrischo/.codex" in aliases


def test_tokenizer_extract_exact_aliases_keeps_alphanum_account_handles():
    from tokenizer import extract_exact_aliases

    aliases = set(extract_exact_aliases("claude1 claude2 claude3 claude4 rotate weekly"))
    for h in ("claude1", "claude2", "claude3", "claude4"):
        assert h in aliases, f"missing handle {h}"


def test_tokenizer_extract_exact_aliases_keeps_hangul_token():
    from tokenizer import extract_exact_aliases

    aliases = set(extract_exact_aliases("조대현 is the Hangul form of Daehyun Cho"))
    assert "조대현" in aliases
    # Latin given+family names also survive — they're distinct identifiers,
    # not stopwords.
    assert "Daehyun" in aliases
    assert "Cho" in aliases
