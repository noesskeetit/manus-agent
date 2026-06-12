"""Persistent shell sessions через tmux. Эквивалент Manus shell_*.

Каждая `session_id` = отдельная tmux-сессия `manus-<id>`. cwd/env сохраняются между вызовами.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


TMP_DIR = Path("/tmp")
PREFIX = "manus"

# session_id'ы созданные текущим процессом — для atexit cleanup
_OWN_SESSIONS: set[str] = set()
_ATEXIT_REGISTERED = False


def _cleanup_own_sessions():
    """atexit: убить все tmux-сессии созданные текущим процессом агента.

    Респектит env MANUS_KEEP_TMUX=true — оставит сессии для отладки.
    """
    if os.environ.get("MANUS_KEEP_TMUX", "").lower() in ("1", "true", "yes"):
        return
    for sid in list(_OWN_SESSIONS):
        try:
            subprocess.run(["tmux", "kill-session", "-t", _tmux_name(sid)],
                            capture_output=True, check=False)
        except Exception:
            pass


# ---------- tmux helpers ----------

def _has_tmux() -> bool:
    return subprocess.run(["which", "tmux"], capture_output=True).returncode == 0


def _tmux_name(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return f"{PREFIX}-{safe or 'default'}"


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def _exists(sid: str) -> bool:
    return _tmux("has-session", "-t", _tmux_name(sid), check=False).returncode == 0


def _ensure(sid: str, cwd: Optional[str] = None) -> None:
    global _ATEXIT_REGISTERED
    if _exists(sid):
        _OWN_SESSIONS.add(sid)  # запомним для cleanup
        return
    args = ["new-session", "-d", "-s", _tmux_name(sid), "-x", "240", "-y", "50"]
    if cwd:
        args.extend(["-c", cwd])
    _tmux(*args)
    _OWN_SESSIONS.add(sid)
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup_own_sessions)
        _ATEXIT_REGISTERED = True
    time.sleep(0.3)


def _workspace_root(ctx: ToolContext) -> Path:
    workspace = getattr(ctx, "workspace", None)
    root = getattr(workspace, "root", None)
    if root is None:
        raise ValueError("workspace root is required for shell tool isolation")
    return Path(root).resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _default_shell_cwd(cwd: Optional[str], ctx: ToolContext) -> Optional[str]:
    root = _workspace_root(ctx)
    if cwd:
        raw_path = Path(cwd).expanduser()
        resolved = (root / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
        if not _is_relative_to(resolved, root):
            raise ValueError(f"shell cwd is outside workspace: {resolved}")
        return str(resolved)
    return str(root)


def _workspace_scoped_session_id(session_id: str, ctx: ToolContext) -> str:
    root = _workspace_root(ctx)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    if session_id.startswith(f"{digest}-"):
        return session_id
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return f"{digest}-{safe or 'default'}"


def _capture(sid: str, lines: int = 200) -> str:
    if not _exists(sid):
        return ""
    r = _tmux("capture-pane", "-pt", _tmux_name(sid), "-S", f"-{lines}", check=False)
    return r.stdout if r.returncode == 0 else ""


# ---------- Tool: shell_exec ----------

class ShellExecArgs(BaseModel):
    session_id: str = Field(..., description="Произвольное имя сессии. Одинаковое имя → та же tmux-сессия (cwd/env сохраняются).")
    command: str = Field(..., description="Bash-команда. Multi-line OK. && для chaining.")
    cwd: Optional[str] = Field(None, description="Рабочая директория (применяется только при создании сессии)")
    timeout_sec: int = Field(120, description="Сколько ждать завершения (default 120s). Если команда длиннее — pollи через shell_view/shell_wait.")


class ShellExecTool(Tool):
    group = "shell"
    name = "shell_exec"
    description = ("Запустить shell-команду в named persistent tmux-сессии. "
                   "Сессия сохраняет cwd/env между вызовами. Используй для long-running процессов, "
                   "цепочек зависимых команд, REPL'ов. Output обрезан до 40k символов.")
    args_schema = ShellExecArgs
    side_effects = True

    def execute(self, args: ShellExecArgs, ctx: ToolContext) -> ToolResult:
        if not _has_tmux():
            return ToolResult(content="ERROR: tmux is not installed (brew install tmux)", is_error=True)

        try:
            tmux_session_id = _workspace_scoped_session_id(args.session_id, ctx)
            cwd = _default_shell_cwd(args.cwd, ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        _ensure(tmux_session_id, cwd=cwd)

        rid = uuid.uuid4().hex[:10]
        out_file = TMP_DIR / f"{PREFIX}-{_tmux_name(tmux_session_id)}-{rid}.out"
        done_file = TMP_DIR / f"{PREFIX}-{_tmux_name(tmux_session_id)}-{rid}.done"

        wrapped = (
            f"{{ {args.command}\n}} > {shlex.quote(str(out_file))} 2>&1; "
            f"printf '%s' $? > {shlex.quote(str(done_file))}"
        )
        _tmux("send-keys", "-t", _tmux_name(tmux_session_id), wrapped, "Enter")

        deadline = time.monotonic() + args.timeout_sec
        while time.monotonic() < deadline:
            if done_file.exists():
                try:
                    code = int(done_file.read_text().strip() or "-1")
                except Exception:
                    code = -1
                output = out_file.read_text() if out_file.exists() else ""
                # cleanup
                for f in (out_file, done_file):
                    try:
                        f.unlink()
                    except Exception:
                        pass
                tail = output[-40_000:]
                trimmed = len(output) > 40_000
                content = f"[exit {code}] session={args.session_id}\n{tail}"
                if trimmed:
                    content += f"\n... [truncated, full {len(output)} chars]"
                return ToolResult(
                    content=content,
                    raw=output,
                    is_error=(code != 0),
                    metadata={"exit_code": code, "session_id": args.session_id,
                              "tmux_session_id": tmux_session_id,
                              "output_size": len(output)},
                )
            time.sleep(0.3)

        partial = out_file.read_text() if out_file.exists() else ""
        return ToolResult(
            content=(f"[TIMEOUT after {args.timeout_sec}s] session={args.session_id} "
                     f"command still running.\n{partial[-30_000:]}\n"
                     f"Use shell_view / shell_wait / shell_kill_process to manage."),
            raw=partial,
            is_error=False,
            metadata={"timeout": True, "session_id": args.session_id, "tmux_session_id": tmux_session_id},
        )


# ---------- shell_view ----------

class ShellViewArgs(BaseModel):
    session_id: str
    lines: int = Field(200, description="Сколько последних строк показать")


class ShellViewTool(Tool):
    group = "shell"
    name = "shell_view"
    description = "Показать последние N строк pane'а tmux-сессии (полезно после timeout или для long-running)."
    args_schema = ShellViewArgs

    def execute(self, args: ShellViewArgs, ctx: ToolContext) -> ToolResult:
        try:
            tmux_session_id = _workspace_scoped_session_id(args.session_id, ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        if not _exists(tmux_session_id):
            return ToolResult(content=f"ERROR: session '{args.session_id}' does not exist", is_error=True)
        out = _capture(tmux_session_id, lines=args.lines)
        return ToolResult(content=f"[{args.session_id}] last {args.lines} lines:\n{out}",
                          metadata={"session_id": args.session_id, "tmux_session_id": tmux_session_id})


# ---------- shell_wait ----------

class ShellWaitArgs(BaseModel):
    session_id: str
    timeout_sec: int = Field(300, description="Сколько максимум ждать idle (default 300)")


class ShellWaitTool(Tool):
    group = "shell"
    name = "shell_wait"
    description = ("Подождать пока tmux-сессия станет idle (heuristic: последняя строка стабильна 3с и похожа на prompt). "
                   "Использовать после shell_exec timeout или для long-running.")
    args_schema = ShellWaitArgs

    def execute(self, args: ShellWaitArgs, ctx: ToolContext) -> ToolResult:
        try:
            tmux_session_id = _workspace_scoped_session_id(args.session_id, ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        if not _exists(tmux_session_id):
            return ToolResult(content=f"ERROR: session '{args.session_id}' does not exist", is_error=True)
        deadline = time.monotonic() + args.timeout_sec
        last_tail = ""
        stable_since: Optional[float] = None
        while time.monotonic() < deadline:
            content = _capture(tmux_session_id, lines=50)
            lines = [l for l in content.splitlines() if l.strip()]
            tail = "\n".join(lines[-3:]) if lines else ""
            if tail == last_tail:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 3:
                    last_line = lines[-1] if lines else ""
                    if last_line.rstrip().endswith(("$", "#", ">", "➜", "❯")):
                        return ToolResult(content=f"[{args.session_id}] idle\nLast line: {last_line}",
                                          metadata={"status": "idle", "session_id": args.session_id,
                                                    "tmux_session_id": tmux_session_id})
            else:
                stable_since = None
                last_tail = tail
            time.sleep(1)
        return ToolResult(
            content=f"[{args.session_id}] still busy after {args.timeout_sec}s.\nLast tail:\n{last_tail}",
            metadata={"status": "timeout", "session_id": args.session_id,
                      "tmux_session_id": tmux_session_id},
        )


# ---------- shell_write_to_process ----------

class ShellWriteArgs(BaseModel):
    session_id: str
    input_text: str = Field(..., description="Что отправить в stdin (например пароль или команду REPL)")
    press_enter: bool = Field(True, description="Нажать Enter после ввода")


class ShellWriteTool(Tool):
    group = "shell"
    name = "shell_write_to_process"
    description = "Отправить ввод в текущий foreground-процесс (REPL, ssh password, и т.д.)"
    args_schema = ShellWriteArgs
    side_effects = True

    def execute(self, args: ShellWriteArgs, ctx: ToolContext) -> ToolResult:
        try:
            tmux_session_id = _workspace_scoped_session_id(args.session_id, ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        if not _exists(tmux_session_id):
            return ToolResult(content=f"ERROR: session '{args.session_id}' does not exist", is_error=True)
        _tmux("send-keys", "-t", _tmux_name(tmux_session_id), "-l", args.input_text)
        if args.press_enter:
            _tmux("send-keys", "-t", _tmux_name(tmux_session_id), "Enter")
        time.sleep(0.3)
        return ToolResult(
            content=f"OK: input sent to {args.session_id}",
            metadata={"session_id": args.session_id, "tmux_session_id": tmux_session_id},
        )


# ---------- shell_kill_process / shell_kill_session ----------

class ShellKillProcessArgs(BaseModel):
    session_id: str


class ShellKillProcessTool(Tool):
    group = "shell"
    name = "shell_kill_process"
    description = "Послать Ctrl+C в текущий foreground-процесс сессии"
    args_schema = ShellKillProcessArgs
    side_effects = True

    def execute(self, args: ShellKillProcessArgs, ctx: ToolContext) -> ToolResult:
        try:
            tmux_session_id = _workspace_scoped_session_id(args.session_id, ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        if not _exists(tmux_session_id):
            return ToolResult(content=f"ERROR: session '{args.session_id}' does not exist", is_error=True)
        _tmux("send-keys", "-t", _tmux_name(tmux_session_id), "C-c")
        time.sleep(0.3)
        return ToolResult(
            content=f"OK: SIGINT sent to {args.session_id}",
            metadata={"session_id": args.session_id, "tmux_session_id": tmux_session_id},
        )


class ShellKillSessionArgs(BaseModel):
    session_id: str


class ShellKillSessionTool(Tool):
    group = "shell"
    name = "shell_kill_session"
    description = "Уничтожить tmux-сессию полностью"
    args_schema = ShellKillSessionArgs
    side_effects = True

    def execute(self, args: ShellKillSessionArgs, ctx: ToolContext) -> ToolResult:
        try:
            tmux_session_id = _workspace_scoped_session_id(args.session_id, ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        if not _exists(tmux_session_id):
            return ToolResult(content=f"OK: session '{args.session_id}' did not exist")
        _tmux("kill-session", "-t", _tmux_name(tmux_session_id))
        return ToolResult(
            content=f"OK: killed {args.session_id}",
            metadata={"session_id": args.session_id, "tmux_session_id": tmux_session_id},
        )


class ShellListArgs(BaseModel):
    pass


class ShellListTool(Tool):
    group = "shell"
    name = "shell_list_sessions"
    description = "Список живых manus-tmux сессий"
    args_schema = ShellListArgs

    def execute(self, args, ctx: ToolContext) -> ToolResult:
        try:
            root = _workspace_root(ctx)
        except ValueError as exc:
            return ToolResult(content=f"ERROR: {exc}", is_error=True)
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        r = _tmux("list-sessions", "-F", "#{session_name}", check=False)
        if r.returncode != 0:
            return ToolResult(content="(no sessions)")
        internal_prefix = f"{PREFIX}-{digest}-"
        names = [
            n.replace(internal_prefix, "", 1)
            for n in r.stdout.strip().splitlines()
            if n.startswith(internal_prefix)
        ]
        return ToolResult(
            content="Sessions: " + (", ".join(names) if names else "(none)"),
            metadata={"session_ids": names, "workspace_scope": digest},
        )


def make_shell_tools() -> list[Tool]:
    return [
        ShellExecTool(),
        ShellViewTool(),
        ShellWaitTool(),
        ShellWriteTool(),
        ShellKillProcessTool(),
        ShellKillSessionTool(),
        ShellListTool(),
    ]
