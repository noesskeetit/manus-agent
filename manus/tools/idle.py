"""idle tool — сигнал что агент закончил работу."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


class IdleArgs(BaseModel):
    summary: str = Field(..., description="Очень краткий итог (1-3 предложения) — что сделано, где артефакты")


class IdleTool(Tool):
    group = "lifecycle"
    always_available = True
    name = "idle"
    description = (
        "Сигнал о завершении задачи. Используй ТОЛЬКО когда:\n"
        "1. todo.md полностью закрыт (все [x]) или явно невыполнимые пункты помечены\n"
        "2. summary.md создан в workspace\n"
        "3. Пользователь уведомлён через message_notify_user\n"
        "После idle агент завершает loop."
    )
    args_schema = IdleArgs

    def execute(self, args: IdleArgs, ctx: ToolContext) -> ToolResult:
        if ctx.agent_state is not None:
            ctx.agent_state.done = True
            ctx.agent_state.final_summary = args.summary
        return ToolResult(content=f"OK: agent entering idle. Summary: {args.summary}",
                          metadata={"idle": True, "summary": args.summary})


def make_idle_tools() -> list[Tool]:
    return [IdleTool()]
