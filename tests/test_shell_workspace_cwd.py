from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from pathlib import Path

from manus.tools.base import ToolContext
from manus.tools import shell
from manus.tools.shell import ShellListArgs, ShellListTool, ShellViewArgs, ShellViewTool, _default_shell_cwd


@dataclass(frozen=True)
class _Workspace:
    root: Path


def test_default_shell_cwd_uses_workspace_root_when_cwd_is_missing(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=_Workspace(root=tmp_path))

    assert _default_shell_cwd(None, ctx) == str(tmp_path)


def test_default_shell_cwd_respects_explicit_cwd_inside_workspace(tmp_path: Path) -> None:
    explicit = tmp_path / "subdir"
    ctx = ToolContext(workspace=_Workspace(root=tmp_path))

    assert _default_shell_cwd(str(explicit), ctx) == str(explicit.resolve())


def test_default_shell_cwd_resolves_relative_cwd_inside_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=_Workspace(root=tmp_path))

    assert _default_shell_cwd("artifacts", ctx) == str((tmp_path / "artifacts").resolve())
    assert _default_shell_cwd(".", ctx) == str(tmp_path.resolve())


def test_default_shell_cwd_rejects_explicit_cwd_outside_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=_Workspace(root=tmp_path / "worker"))

    try:
        _default_shell_cwd(str(tmp_path / "outside"), ctx)
    except ValueError as exc:
        assert "outside workspace" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_default_shell_cwd_requires_workspace_root() -> None:
    ctx = ToolContext(workspace=None)

    try:
        _default_shell_cwd(None, ctx)
    except ValueError as exc:
        assert "workspace root is required" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_shell_session_id_is_scoped_by_workspace_root(tmp_path: Path) -> None:
    ctx_a = ToolContext(workspace=_Workspace(root=tmp_path / "worker-a"))
    ctx_b = ToolContext(workspace=_Workspace(root=tmp_path / "worker-b"))

    scoped_a = shell._workspace_scoped_session_id("main", ctx_a)
    scoped_b = shell._workspace_scoped_session_id("main", ctx_b)

    assert scoped_a != scoped_b
    assert scoped_a.endswith("-main")
    assert scoped_b.endswith("-main")
    assert shell._workspace_scoped_session_id("main", ctx_a) == scoped_a
    assert shell._workspace_scoped_session_id(scoped_a, ctx_a) == scoped_a


def test_shell_view_uses_workspace_scoped_session_id(monkeypatch, tmp_path: Path) -> None:
    ctx = ToolContext(workspace=_Workspace(root=tmp_path / "worker-a"))
    seen: dict[str, str] = {}

    def fake_exists(session_id: str) -> bool:
        seen["exists"] = session_id
        return True

    def fake_capture(session_id: str, *, lines: int) -> str:
        seen["capture"] = session_id
        seen["lines"] = str(lines)
        return "ok\n"

    monkeypatch.setattr(shell, "_exists", fake_exists)
    monkeypatch.setattr(shell, "_capture", fake_capture)

    result = ShellViewTool().execute(ShellViewArgs(session_id="main", lines=7), ctx)

    scoped = shell._workspace_scoped_session_id("main", ctx)
    assert seen == {"exists": scoped, "capture": scoped, "lines": "7"}
    assert result.metadata["session_id"] == "main"
    assert result.metadata["tmux_session_id"] == scoped


def test_shell_list_sessions_filters_to_current_workspace(monkeypatch, tmp_path: Path) -> None:
    ctx = ToolContext(workspace=_Workspace(root=tmp_path / "worker-a"))
    other_ctx = ToolContext(workspace=_Workspace(root=tmp_path / "worker-b"))
    current_main = shell._workspace_scoped_session_id("main", ctx)
    current_logs = shell._workspace_scoped_session_id("logs", ctx)
    other_main = shell._workspace_scoped_session_id("main", other_ctx)

    def fake_tmux(*args: str, check: bool = True):
        assert args == ("list-sessions", "-F", "#{session_name}")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"{shell.PREFIX}-{current_main}\n"
                f"{shell.PREFIX}-{other_main}\n"
                f"{shell.PREFIX}-{current_logs}\n"
            ),
        )

    monkeypatch.setattr(shell, "_tmux", fake_tmux)

    result = ShellListTool().execute(ShellListArgs(), ctx)

    assert result.content == "Sessions: main, logs"
    assert result.metadata["session_ids"] == ["main", "logs"]
