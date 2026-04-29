"""Tools: list_skills, activate_skill, deactivate_skill (B1 progressive disclosure)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


class ListSkillsArgs(BaseModel):
    pass


class ListSkillsTool(Tool):
    group = "skills"
    read_only = True
    plan_safe = True
    always_available = True
    name = "list_skills"
    description = (
        "Список всех available skills с tier-1 metadata (~30-50 tok каждый). "
        "После выбора — activate_skill(name) для load tier-2 instructions."
    )
    args_schema = ListSkillsArgs

    def execute(self, args, ctx: ToolContext) -> ToolResult:
        from ..skills_loader import discover_skills
        skills = discover_skills()
        if not skills:
            return ToolResult(content="(no skills available)")
        active = []
        if ctx.agent_state is not None:
            active = list(getattr(ctx.agent_state, "activated_skills", []))
        lines = [f"# {len(skills)} skills available"]
        for name, skill in sorted(skills.items()):
            mark = "[●]" if name in active else "[ ]"
            lines.append(f"{mark} {skill.metadata.short_metadata}")
            if skill.metadata.triggers:
                lines.append(f"      triggers: {', '.join(skill.metadata.triggers[:6])}")
        if active:
            lines.append(f"\nactive: {', '.join(active)}")
        return ToolResult(content="\n".join(lines))


class ActivateSkillArgs(BaseModel):
    name: str = Field(..., description="Имя skill (см. list_skills)")


class ActivateSkillTool(Tool):
    group = "skills"
    plan_safe = True
    always_available = True
    name = "activate_skill"
    description = (
        "Активировать skill — tier-2 instructions попадают в context. "
        "Опционально может изменить active_groups (если skill их декларирует). "
        "Лимит 3 активных skills одновременно (lru drop)."
    )
    args_schema = ActivateSkillArgs
    side_effects = True

    def execute(self, args: ActivateSkillArgs, ctx: ToolContext) -> ToolResult:
        from ..skills_loader import discover_skills
        skills = discover_skills()
        if args.name not in skills:
            return ToolResult(
                content=f"ERROR: skill '{args.name}' not found. Available: "
                        f"{', '.join(sorted(skills.keys()))}",
                is_error=True,
            )
        skill = skills[args.name]
        if ctx.agent_state is None:
            return ToolResult(content="ERROR: no agent_state available", is_error=True)
        active = list(getattr(ctx.agent_state, "activated_skills", []) or [])
        if args.name in active:
            return ToolResult(content=f"OK: skill '{args.name}' already active")
        # LRU drop
        while len(active) >= 3:
            dropped = active.pop(0)
            # Не вызываем notification — просто drop
        active.append(args.name)
        ctx.agent_state.activated_skills = active

        # Skill может рекомендовать active_groups — РАСШИРЯЕМ существующие (union),
        # никогда не сужаем (это ломало async sub-agents в research workflow).
        # User's `--groups` остаётся priority — skill только добавляет.
        info_extra = ""
        if skill.metadata.active_groups:
            current = ctx.agent_state.active_groups
            if current is None:
                # Если у user не было restrictions — оставляем None (все группы доступны)
                pass
            else:
                # Union: skill добавляет к user's active_groups, не убирает
                union = sorted(set(current) | set(skill.metadata.active_groups))
                if union != list(current):
                    ctx.agent_state.active_groups = union
                    added = [g for g in skill.metadata.active_groups if g not in current]
                    if added:
                        info_extra = f"; extended active_groups (+ {added})"

        return ToolResult(
            content=(f"OK: activated skill '{args.name}' (v{skill.metadata.version}). "
                     f"Tier-2 instructions injected into next context{info_extra}."),
            metadata={"name": args.name, "active_skills": active},
        )


class DeactivateSkillArgs(BaseModel):
    name: str


class DeactivateSkillTool(Tool):
    group = "skills"
    plan_safe = True
    always_available = True
    name = "deactivate_skill"
    description = "Удалить skill из активных (tier-2 instructions выгружаются)."
    args_schema = DeactivateSkillArgs
    side_effects = True

    def execute(self, args: DeactivateSkillArgs, ctx: ToolContext) -> ToolResult:
        if ctx.agent_state is None:
            return ToolResult(content="ERROR: no agent_state available", is_error=True)
        active = list(getattr(ctx.agent_state, "activated_skills", []) or [])
        if args.name not in active:
            return ToolResult(content=f"OK: skill '{args.name}' was not active")
        active.remove(args.name)
        ctx.agent_state.activated_skills = active
        return ToolResult(content=f"OK: deactivated skill '{args.name}'")


def make_skills_tools() -> list[Tool]:
    return [ListSkillsTool(), ActivateSkillTool(), DeactivateSkillTool()]
