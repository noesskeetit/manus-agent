from __future__ import annotations

from pathlib import Path

from manus.agent import Agent
from manus.tools import build_default_registry
from manus.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "worker"
    root.mkdir()
    return Workspace(task_id="hard-mask-test", root=root, task_text="test")


def test_hard_tool_mask_limits_specs_to_active_groups(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    monkeypatch.setenv("MANUS_HARD_TOOL_MASK", "1")
    registry = build_default_registry()

    agent = Agent(
        workspace=_workspace(tmp_path),
        registry=registry,
        active_groups=["file"],
    )

    assert len(agent._all_specs) == len(registry.filter_specs(["file"]))
    assert len(agent._all_specs) < len(registry.to_openai_specs())

    agent.set_active_groups(["file", "memory"])

    assert len(agent._all_specs) == len(registry.filter_specs(["file", "memory"]))


def test_soft_tool_mask_keeps_stable_full_specs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    monkeypatch.delenv("MANUS_HARD_TOOL_MASK", raising=False)
    registry = build_default_registry()

    agent = Agent(
        workspace=_workspace(tmp_path),
        registry=registry,
        active_groups=["file"],
    )

    assert len(agent._all_specs) == len(registry.to_openai_specs())
