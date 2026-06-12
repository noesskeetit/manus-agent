from __future__ import annotations

from types import SimpleNamespace

from manus.agent import Agent
from manus.tools.base import ToolResult


class _NoDumpWorkspace:
    root = None

    def dump_observation(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("read_observation results must not be dumped again")


class _NoopContext:
    def auto_pin(self, value: str) -> None:
        raise AssertionError(f"read_observation results must not be pinned again: {value}")


def test_large_read_observation_result_is_not_dumped_again() -> None:
    agent = SimpleNamespace(
        state=SimpleNamespace(iteration=3),
        workspace=_NoDumpWorkspace(),
        context=_NoopContext(),
    )
    body = "observation body\n" * 500

    result = Agent._maybe_dump_observation(
        agent,
        ToolResult(content=body),
        "read_observation",
        tc_id="tool-123456",
    )

    assert "Large read_observation output not re-saved" in result
    assert "use read_observation" not in result
    assert body[:100] in result
