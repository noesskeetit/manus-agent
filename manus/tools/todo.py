"""Structured todo_* tools — Claude Code TodoWrite паттерн.

Имя `todo_*` (а не `task_*`) чтобы не конфликтовать с PAC1 `task_context/task_answer`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


# ---------- Helpers ----------

def _store(ctx: ToolContext):
    """Lazy init TaskStore + auto-rerender todo.md."""
    from ..todo_tracker import TaskStore
    ws = ctx.workspace
    tasks_file = ws.root / "tasks.json"
    return TaskStore(tasks_file), ws


def _rerender(store, ws):
    """Auto-update todo.md after each todo_* call (UI compat)."""
    try:
        md = store.render_markdown(task_text=ws.task_text, task_id=ws.task_id)
        ws.todo.write_text(md, encoding="utf-8")
    except Exception:
        pass


# ---------- Schemas ----------

class TodoCreateArgs(BaseModel):
    subject: str = Field(..., description="Краткое название задачи (1 строка)")
    description: str = Field("", description="Подробности (опционально)")
    parent: Optional[str] = Field(None, description="ID parent task для subtask")
    blocked_by: Optional[list[str]] = Field(None, description="IDs тасков которые должны быть completed раньше")


class TodoUpdateArgs(BaseModel):
    id: str
    status: Optional[Literal["pending", "in_progress", "completed", "blocked"]] = None
    subject: Optional[str] = None
    description: Optional[str] = None
    add_blocks: Optional[list[str]] = Field(None, description="Tasks которые БЛОКИРУЮТСЯ этим (forward-link)")
    add_blocked_by: Optional[list[str]] = Field(None, description="Tasks которые блокируют этот (back-link)")


class TodoGetArgs(BaseModel):
    id: str


class TodoListArgs(BaseModel):
    status: Optional[Literal["pending", "in_progress", "completed", "blocked"]] = None
    parent: Optional[str] = None


class TodoDeleteArgs(BaseModel):
    id: str


# ---------- Tools ----------

class TodoCreateTool(Tool):
    group = "todo"
    plan_safe = True
    name = "todo_create"
    description = (
        "Создать structured todo task. Возвращает ID. "
        "Используй вместо file_write/str_replace для управления планом — "
        "todo.md auto-renders из tasks.json. "
        "Поддерживает parent/subtask hierarchy и blocked_by dependencies."
    )
    args_schema = TodoCreateArgs
    side_effects = True

    def execute(self, args: TodoCreateArgs, ctx: ToolContext) -> ToolResult:
        store, ws = _store(ctx)
        t = store.create(args.subject, args.description, args.parent, args.blocked_by)
        _rerender(store, ws)
        return ToolResult(
            content=f"OK: created `{t.id}` [{t.status}] {t.subject}",
            metadata={"id": t.id},
        )


class TodoUpdateTool(Tool):
    group = "todo"
    plan_safe = True
    name = "todo_update"
    description = (
        "Обновить task: status, subject, description, dependencies. "
        "После update todo.md auto-rerendered. "
        "Status: pending|in_progress|completed|blocked."
    )
    args_schema = TodoUpdateArgs
    side_effects = True

    def execute(self, args: TodoUpdateArgs, ctx: ToolContext) -> ToolResult:
        store, ws = _store(ctx)
        t = store.update(
            args.id, status=args.status, subject=args.subject,
            description=args.description,
            add_blocks=args.add_blocks, add_blocked_by=args.add_blocked_by,
        )
        if t is None:
            return ToolResult(content=f"ERROR: task `{args.id}` not found", is_error=True)
        _rerender(store, ws)
        return ToolResult(
            content=f"OK: updated `{t.id}` [{t.status}] {t.subject}",
            metadata={"id": t.id, "status": t.status},
        )


class TodoGetTool(Tool):
    group = "todo"
    read_only = True
    plan_safe = True
    name = "todo_get"
    description = "Получить полную инфу по task (включая deps)."
    args_schema = TodoGetArgs

    def execute(self, args: TodoGetArgs, ctx: ToolContext) -> ToolResult:
        store, _ = _store(ctx)
        t = store.get(args.id)
        if t is None:
            return ToolResult(content=f"ERROR: task `{args.id}` not found", is_error=True)
        from dataclasses import asdict
        import json as _j
        return ToolResult(content=_j.dumps(asdict(t), ensure_ascii=False, indent=2))


class TodoListTool(Tool):
    group = "todo"
    read_only = True
    plan_safe = True
    name = "todo_list"
    description = (
        "Список tasks. Можно фильтровать по status и/или parent. "
        "Без фильтров — все. Возвращает компактную таблицу."
    )
    args_schema = TodoListArgs

    def execute(self, args: TodoListArgs, ctx: ToolContext) -> ToolResult:
        store, _ = _store(ctx)
        tasks = store.list(status=args.status, parent=args.parent)
        if not tasks:
            return ToolResult(content="(no tasks match filter)")
        lines = [f"# {len(tasks)} tasks"]
        for t in tasks:
            blocked_by = f" ⛔{','.join(t.blocked_by)}" if t.blocked_by else ""
            lines.append(f"  {t.id}  [{t.status:11s}]  {t.subject[:80]}{blocked_by}")
        return ToolResult(content="\n".join(lines))


class TodoDeleteTool(Tool):
    group = "todo"
    plan_safe = True
    name = "todo_delete"
    description = "Удалить task. Refs из других tasks автоматически чистятся."
    args_schema = TodoDeleteArgs
    side_effects = True

    def execute(self, args: TodoDeleteArgs, ctx: ToolContext) -> ToolResult:
        store, ws = _store(ctx)
        if store.delete(args.id):
            _rerender(store, ws)
            return ToolResult(content=f"OK: deleted `{args.id}`")
        return ToolResult(content=f"ERROR: task `{args.id}` not found", is_error=True)


def make_todo_tools() -> list[Tool]:
    return [
        TodoCreateTool(),
        TodoUpdateTool(),
        TodoGetTool(),
        TodoListTool(),
        TodoDeleteTool(),
    ]
