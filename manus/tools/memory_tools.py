"""Memory tools для агента: recall, read_observation, write_journal.

По советам agent-memory-expert: дать агенту прямой доступ к диску как памяти.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


# ---------- recall ----------

class RecallArgs(BaseModel):
    query: str = Field(..., description="Регулярка/строка для поиска по journal.md, todo.md, observations/*")
    max_hits: int = Field(20, description="Сколько hits максимум")


class RecallTool(Tool):
    group = "memory"
    read_only = True
    plan_safe = True
    name = "recall"
    description = (
        "Поиск по сохранённой памяти: journal.md + todo.md + observations/*. "
        "Используй когда забыл что делал N turn'ов назад или нужен факт из прошлого. "
        "Возвращает path:line — чтобы дальше дёрнуть read_observation."
    )
    args_schema = RecallArgs

    def execute(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        ws = ctx.workspace
        all_hits: list[str] = []

        # journal.md
        if ws.journal.exists():
            for i, line in enumerate(ws.journal.read_text(encoding="utf-8").splitlines()):
                if args.query.lower() in line.lower():
                    all_hits.append(f"journal.md:{i}: {line.strip()[:200]}")
                    if len(all_hits) >= args.max_hits:
                        break

        # todo.md
        if len(all_hits) < args.max_hits and ws.todo.exists():
            for i, line in enumerate(ws.todo.read_text(encoding="utf-8").splitlines()):
                if args.query.lower() in line.lower():
                    all_hits.append(f"todo.md:{i}: {line.strip()[:200]}")
                    if len(all_hits) >= args.max_hits:
                        break

        # observations
        if len(all_hits) < args.max_hits:
            obs_hits = ws.grep_observations(args.query, max_hits=args.max_hits - len(all_hits))
            for h in obs_hits:
                all_hits.append(f"{h['path']}:{h['line_no']}: {h['line']}")

        if not all_hits:
            return ToolResult(content=f"No matches for: {args.query}")
        return ToolResult(content=f"Found {len(all_hits)} hits:\n" + "\n".join(all_hits))


# ---------- read_observation ----------

class ReadObsArgs(BaseModel):
    path: str = Field(..., description="Путь к observation (относительный к workspace или абсолютный)")
    start_line: int = Field(0)
    end_line: Optional[int] = Field(None)


class ReadObservationTool(Tool):
    group = "memory"
    read_only = True
    plan_safe = True
    name = "read_observation"
    description = (
        "Прочитать сохранённый observation (большой dump'нутый tool result или web page). "
        "Поддерживает .gz. Используй после recall чтобы достать конкретные строки."
    )
    args_schema = ReadObsArgs

    def execute(self, args: ReadObsArgs, ctx: ToolContext) -> ToolResult:
        try:
            txt = ctx.workspace.read_observation(args.path, start_line=args.start_line, end_line=args.end_line)
        except FileNotFoundError:
            return ToolResult(content=f"ERROR: observation not found: {args.path}", is_error=True)
        except Exception as e:
            return ToolResult(content=f"ERROR: {e}", is_error=True)

        # Если очень большой кусок — обрежем head/tail
        if len(txt) > 30_000:
            head = txt[:20_000]
            tail = txt[-5_000:]
            txt = f"{head}\n\n... [middle truncated, total {len(txt)} chars] ...\n\n{tail}"
        return ToolResult(content=f"[{args.path}]\n{txt}")


# ---------- write_journal ----------

class WriteJournalArgs(BaseModel):
    entry: str = Field(..., description="Что записать (insight, decision, blocker)")


class WriteJournalTool(Tool):
    group = "memory"
    plan_safe = True   # journal — для аудита plan'а тоже
    name = "write_journal"
    description = (
        "Дописать запись в journal.md (это твоя long-term memory задачи). "
        "Используй для архитектурных решений, важных открытий, lessons learned. "
        "НЕ для todo state — для этого file_str_replace на todo.md."
    )
    args_schema = WriteJournalArgs
    side_effects = True

    def execute(self, args: WriteJournalArgs, ctx: ToolContext) -> ToolResult:
        ctx.workspace.append_journal(args.entry)
        return ToolResult(content=f"OK: appended {len(args.entry)} chars to journal.md")


def make_memory_tools() -> list[Tool]:
    return [RecallTool(), ReadObservationTool(), WriteJournalTool()]
