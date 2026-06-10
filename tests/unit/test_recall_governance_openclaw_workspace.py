from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_routes_recall_reexports_openclaw_workspace_seams():
    import routes.recall as recall_route
    from recall_governance import openclaw_workspace

    assert (
        recall_route._OPENCLAW_WORKSPACE_INSTRUCTION_RE
        is openclaw_workspace.OPENCLAW_WORKSPACE_INSTRUCTION_RE
    )
    assert recall_route._OPENCLAW_QUERY_TOKENS is openclaw_workspace.OPENCLAW_QUERY_TOKENS


def test_openclaw_workspace_instruction_classifier_uses_path_and_metadata():
    from recall_governance import openclaw_workspace

    assert openclaw_workspace.is_openclaw_workspace_instruction_result(
        {"path": "/Users/chrischo/.openclaw/workspace-liz/AGENTS.md"}
    )
    assert openclaw_workspace.is_openclaw_workspace_instruction_result(
        {"metadata": {"source_path": "/Users/chrischo/.openclaw/workspace-sage/TOOLS.md"}}
    )
    assert not openclaw_workspace.is_openclaw_workspace_instruction_result(
        {"path": "/Users/chrischo/.openclaw/workspace-liz/memories.md"}
    )
    assert not openclaw_workspace.is_openclaw_workspace_instruction_result(
        {"path": "/Users/chrischo/.hermes/profiles/liz/AGENTS.md"}
    )


def test_routes_wrapper_preserves_openclaw_workspace_classifier():
    from routes.recall import _is_openclaw_workspace_instruction_result

    assert _is_openclaw_workspace_instruction_result(
        {"metadata": {"path": "/Users/chrischo/.openclaw/workspace-market/tools.md"}}
    )
    assert not _is_openclaw_workspace_instruction_result(
        {"metadata": {"path": "/Users/chrischo/server/brain/AGENTS.md"}}
    )
