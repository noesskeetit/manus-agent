"""Plan mode lifecycle tools (B2): EnterPlanMode / ExitPlanMode + Critic gate (B3) wiring."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


class EnterPlanModeArgs(BaseModel):
    pass


class EnterPlanModeTool(Tool):
    group = "lifecycle"
    plan_safe = True
    always_available = True
    name = "enter_plan_mode"
    description = (
        "Перевести агента в PLAN mode — only read_only/plan_safe tools allowed. "
        "Используй когда задача сложная и нужно сначала разработать план, "
        "не делая destructive actions. После plan'а — exit_plan_mode(plan_file)."
    )
    args_schema = EnterPlanModeArgs
    side_effects = True

    def execute(self, args, ctx: ToolContext) -> ToolResult:
        if ctx.agent_state is None:
            return ToolResult(content="ERROR: no agent_state", is_error=True)
        prev = ctx.agent_state.mode
        ctx.agent_state.mode = "PLAN"
        return ToolResult(
            content=f"OK: mode {prev} → PLAN. "
                    "Now only read-only and plan-safe tools work. "
                    "Use file_write/file_str_replace on `plan.md` (plan_safe), "
                    "todo_create/update for tracking. After plan ready — exit_plan_mode().",
            metadata={"mode": "PLAN"},
        )


class ExitPlanModeArgs(BaseModel):
    plan_file: str = Field("plan.md", description="Путь к plan-файлу в workspace")
    summary: str = Field("", description="Короткое summary плана (1-3 предложения)")


class ExitPlanModeTool(Tool):
    group = "lifecycle"
    plan_safe = True
    always_available = True
    name = "exit_plan_mode"
    description = (
        "Выйти из PLAN mode → EXEC. После этого все tools доступны. "
        "Аргумент plan_file — где лежит план, summary — короткая сводка."
    )
    args_schema = ExitPlanModeArgs
    side_effects = True

    def execute(self, args: ExitPlanModeArgs, ctx: ToolContext) -> ToolResult:
        if ctx.agent_state is None:
            return ToolResult(content="ERROR: no agent_state", is_error=True)
        prev = ctx.agent_state.mode
        ctx.agent_state.mode = "EXEC"
        # Опционально читаем plan
        plan_excerpt = ""
        try:
            from pathlib import Path
            p = Path(args.plan_file)
            if not p.is_absolute():
                p = ctx.workspace.root / p
            if p.exists():
                plan_excerpt = p.read_text(encoding="utf-8")[:1500]
        except Exception:
            pass
        return ToolResult(
            content=f"OK: mode {prev} → EXEC. Summary: {args.summary[:200]}\n"
                    f"--- plan excerpt ---\n{plan_excerpt}",
            metadata={"mode": "EXEC", "summary": args.summary},
        )


def make_lifecycle_tools() -> list[Tool]:
    return [EnterPlanModeTool(), ExitPlanModeTool()]
