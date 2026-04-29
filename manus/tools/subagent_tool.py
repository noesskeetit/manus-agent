"""Tool: spawn_subagent — даёт main agent'у возможность делегировать research-подзадачу."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


# Карта role → default active_groups (Conservative defaults)
ROLE_DEFAULTS: dict[str, dict] = {
    "planner": {
        "active_groups": ["file", "memory", "lifecycle", "communication"],
        "max_iterations": 30,
    },
    "executor": {
        "active_groups": None,  # all groups allowed
        "max_iterations": 60,
    },
    "researcher": {
        "active_groups": ["research", "file", "memory", "lifecycle", "communication"],
        "max_iterations": 50,
    },
    "critic": {
        "active_groups": ["file", "memory", "lifecycle", "communication"],
        "max_iterations": 15,
    },
    "debugger": {
        "active_groups": ["shell", "file", "memory", "lifecycle", "communication"],
        "max_iterations": 40,
    },
}


class SpawnSubagentArgs(BaseModel):
    task: str = Field(..., description="Чёткая постановка задачи для sub-agent'а")
    role: Optional[Literal["planner", "executor", "researcher", "critic", "debugger"]] = Field(
        None,
        description="Specialist role с собственным system prompt и preset active_groups. "
                    "Если не указано — generic sub-agent. "
                    "planner=декомпозиция плана. executor=одна phase. researcher=сбор инфы. "
                    "critic=проверка proposed action. debugger=диагностика bug.",
    )
    in_scope: Optional[list[str]] = Field(None, description="Что входит в скоуп подзадачи")
    out_of_scope: Optional[list[str]] = Field(None, description="Что НЕ входит (явные ограничения)")
    deliverables: Optional[list[str]] = Field(None, description="Что должно быть на выходе (артефакты)")
    model: Optional[str] = Field(None, description="Модель для sub-agent'а (qwen-coder|minimax|glm)")
    max_iterations: Optional[int] = Field(None, description="Макс iterations (default по роли)")
    timeout_sec: int = Field(1800, description="Hard timeout (секунды)")
    active_groups_override: Optional[list[str]] = Field(
        None,
        description="ЯВНО переопределить active_groups (wins над role defaults)",
    )


class SpawnSubagentTool(Tool):
    group = "subagent"
    name = "spawn_subagent"
    description = (
        "Запустить sub-agent в изолированном workspace для investigation/research. "
        "ИСПОЛЬЗОВАТЬ ТОЛЬКО для read-only задач (research, analysis, document review) или "
        "независимых исследований по разным темам параллельно. "
        "НЕ ИСПОЛЬЗОВАТЬ для side-effect операций (publish, deploy, write to shared paths) — "
        "делай их сам в основном loop. "
        "Sub-agent вернёт summary 200-400 слов + список артефактов."
    )
    args_schema = SpawnSubagentArgs
    side_effects = True
    timeout_sec = 1900

    def execute(self, args: SpawnSubagentArgs, ctx: ToolContext) -> ToolResult:
        # Lazy import чтобы избежать circular
        from ..subagent import spawn_subagent

        scope = {
            "in_scope": args.in_scope or [],
            "out_of_scope": args.out_of_scope or ["modifying parent's todo.md/journal.md",
                                                    "message_ask_user", "publishing/deploy"],
            "deliverables": args.deliverables or ["summary.md in own workspace"],
        }
        # Resolve role defaults
        role_cfg = ROLE_DEFAULTS.get(args.role, {}) if args.role else {}
        max_iter = args.max_iterations or role_cfg.get("max_iterations", 40)
        # active_groups: explicit override wins, else role default, else generic (None)
        active_groups = (
            args.active_groups_override
            if args.active_groups_override is not None
            else role_cfg.get("active_groups")
        )
        result = spawn_subagent(
            parent_workspace_path=ctx.workspace.root,
            task=args.task,
            scope=scope,
            model=args.model,
            max_iterations=max_iter,
            timeout_sec=args.timeout_sec,
            role=args.role,
            active_groups=active_groups,
        )
        body = (
            f"Sub-agent {result.sub_id}: status={result.status}, "
            f"duration={result.duration_sec:.1f}s, iters={result.findings.get('iterations', '?')}\n"
            f"Workspace: {result.workspace_path}\n"
            f"Artifacts ({len(result.artifacts)}): " +
            ", ".join(result.artifacts[:8]) +
            ("..." if len(result.artifacts) > 8 else "") + "\n\n"
            f"Summary:\n{result.summary[:3000]}"
        )
        if result.error:
            body += f"\n\nError: {result.error}"
        return ToolResult(
            content=body, is_error=(result.status != "completed"),
            artifacts=result.artifacts,
            metadata={"sub_id": result.sub_id, "status": result.status,
                      "duration_sec": result.duration_sec},
        )


# ---------- C1: Async sub-agents ----------

class SpawnSubagentAsyncArgs(BaseModel):
    task: str
    role: Optional[Literal["planner", "executor", "researcher", "critic", "debugger"]] = None
    in_scope: Optional[list[str]] = None
    out_of_scope: Optional[list[str]] = None
    deliverables: Optional[list[str]] = None
    model: Optional[str] = None
    max_iterations: Optional[int] = None
    timeout_sec: int = 1800


class SpawnSubagentAsyncTool(Tool):
    group = "subagent"
    name = "spawn_subagent_async"
    description = (
        "Spawn sub-agent В ФОНЕ — main agent НЕ блокируется. Возвращает sub_id моментально. "
        "Result автоматически инжектируется в context когда sub-agent завершится. "
        "Используй для parallel research / analysis (Manus Wide Research style). "
        "Не подходит для side-effect actions (publish, deploy)."
    )
    args_schema = SpawnSubagentAsyncArgs
    side_effects = True

    def execute(self, args: SpawnSubagentAsyncArgs, ctx: ToolContext) -> ToolResult:
        from ..subagent import spawn_subagent_async
        scope = {
            "in_scope": args.in_scope or [],
            "out_of_scope": args.out_of_scope or ["modifying parent files",
                                                   "publishing/deploy"],
            "deliverables": args.deliverables or ["summary in own workspace"],
        }
        role_cfg = ROLE_DEFAULTS.get(args.role, {}) if args.role else {}
        max_iter = args.max_iterations or role_cfg.get("max_iterations", 40)
        active_groups = role_cfg.get("active_groups")
        info = spawn_subagent_async(
            parent_workspace_path=ctx.workspace.root,
            task=args.task,
            scope=scope,
            model=args.model,
            max_iterations=max_iter,
            timeout_sec=args.timeout_sec,
            role=args.role,
            active_groups=active_groups,
        )
        # Регистрируем в agent_state для polling
        if ctx.agent_state is not None:
            pending = list(getattr(ctx.agent_state, "async_subagents", []) or [])
            pending.append(info)
            ctx.agent_state.async_subagents = pending
        return ToolResult(
            content=(f"OK: spawned async sub-agent. sub_id={info['sub_id']} "
                     f"(role={args.role or 'generic'}, timeout={args.timeout_sec}s). "
                     "Continue working. Result will be injected when ready."),
            metadata={"sub_id": info["sub_id"], "pid": info["pid"]},
        )


class SubagentCheckArgs(BaseModel):
    sub_id: str = Field(..., description="ID async sub-agent'а (от spawn_subagent_async)")


class SubagentCheckTool(Tool):
    group = "subagent"
    read_only = True
    name = "subagent_check"
    description = "Проверить статус async sub-agent (running / completed / timeout)."
    args_schema = SubagentCheckArgs

    def execute(self, args: SubagentCheckArgs, ctx: ToolContext) -> ToolResult:
        from ..subagent import check_async_subagent
        if ctx.agent_state is None:
            return ToolResult(content="ERROR: no agent_state", is_error=True)
        pending = getattr(ctx.agent_state, "async_subagents", []) or []
        for info in pending:
            if info.get("sub_id") == args.sub_id:
                result = check_async_subagent(info)
                if result is None:
                    return ToolResult(content=f"sub_id={args.sub_id}: still running")
                return ToolResult(
                    content=f"sub_id={args.sub_id}: {result.status} duration={result.duration_sec:.0f}s\n"
                            f"summary: {result.summary[:1000]}",
                    metadata={"sub_id": args.sub_id, "status": result.status},
                )
        return ToolResult(content=f"ERROR: sub_id={args.sub_id} not found", is_error=True)


def make_subagent_tools() -> list[Tool]:
    return [SpawnSubagentTool(), SpawnSubagentAsyncTool(), SubagentCheckTool()]
