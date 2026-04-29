"""Tool registry, base class, схемы, валидация и idempotency.

Каждый инструмент:
1. Описывает свои аргументы Pydantic-моделью (strict, extra='forbid')
2. Имеет execute(args, ctx) → ToolResult
3. Регистрируется в Registry, который генерит OpenAI-compat tool_specs
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("manus.tools")


# ---------- Контекст инструмента ----------

@dataclass
class ToolContext:
    """То, что инструменту нужно от агента: workspace, конфиг, state."""
    workspace: Any                    # Workspace
    agent_state: Any = None           # AgentState (optional, для tool'ов которым важен текущий план)
    cancel_flag: Any = None           # threading.Event для interrupt


# ---------- Результат инструмента ----------

@dataclass
class ToolResult:
    """То, что возвращает инструмент. content идёт обратно в LLM, raw — на диск/лог."""
    content: str                      # текст для LLM (компактный, ≤2k tokens)
    raw: Any = None                   # полный output (если большой — на диск дампится, в content только TL;DR + path)
    is_error: bool = False
    artifacts: list[str] = field(default_factory=list)  # пути к созданным файлам
    metadata: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


# ---------- Базовый класс инструмента ----------

class Tool(ABC):
    """Базовый класс. Каждый concrete tool наследует и реализует execute()."""

    name: str = ""
    description: str = ""
    args_schema: type[BaseModel] | None = None
    side_effects: bool = False        # True если tool изменяет внешний мир (post in TG, delete file, etc.)
    timeout_sec: int = 120
    group: str = "core"               # для logit masking / phase-aware фильтрации
    always_available: bool = False    # idle / message_notify_user — никогда не маскируются
    read_only: bool = False           # strictly не мутирует ничего (для plan mode)
    plan_safe: bool = False           # OK вызывать в plan mode (read_only OR locally-planning writes)
    requires_critic: bool = False     # перед execution spawn'ить critic subagent (B3)

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} must define `name`")
        if not self.description:
            raise ValueError(f"{type(self).__name__} must define `description`")

    @abstractmethod
    def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Выполнить инструмент. args — уже валидированные Pydantic-аргументы."""

    def to_openai_spec(self) -> dict[str, Any]:
        """Сгенерить OpenAI-compat tool spec для chat.completions."""
        if self.args_schema is None:
            params: dict[str, Any] = {"type": "object", "properties": {}}
        else:
            params = self.args_schema.model_json_schema()
            # Pydantic кладёт title и кучу мета — OpenAI не любит лишнее
            params.pop("title", None)
            params.setdefault("type", "object")
            # Удаляем ссылки на $defs если есть — OpenAI плохо ест
            if "$defs" in params:
                # для простых тулов это OK
                pass
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }


# ---------- Registry ----------

class ToolRegistry:
    """Хранит зарегистрированные инструменты, выполняет валидацию + execute."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._idempotency_cache: dict[str, ToolResult] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def clear_idempotency_cache(self) -> None:
        """Очистить кэш идемпотентности (например, между benchmark trials)."""
        self._idempotency_cache.clear()

    def register_many(self, tools: list[Tool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_specs(self, names: Optional[list[str]] = None) -> list[dict]:
        tools = self._tools.values() if names is None else (
            self._tools[n] for n in names if n in self._tools
        )
        return [t.to_openai_spec() for t in tools]

    # ---------- Группы / маскирование ----------

    def groups(self) -> dict[str, list[str]]:
        """Группировка тулов по `group` атрибуту."""
        out: dict[str, list[str]] = {}
        for name, t in self._tools.items():
            out.setdefault(t.group, []).append(name)
        for g in out:
            out[g].sort()
        return out

    def names_in_groups(self, allowed_groups: list[str]) -> list[str]:
        """Все имена тулов из перечисленных групп + `always_available` тулы."""
        s = set(allowed_groups)
        names: list[str] = []
        for name, t in self._tools.items():
            if t.always_available or t.group in s:
                names.append(name)
        return sorted(names)

    def filter_specs(self, allowed_groups: Optional[list[str]] = None,
                     extra_names: Optional[list[str]] = None) -> list[dict]:
        """Возвращает OpenAI tool_specs только для allowed_groups + always_available + extra_names.

        Используй когда хочешь сменить prefix (это инвалидирует KV-cache).
        Альтернатива — отдавать ВСЕ specs и направлять модель через system prompt
        («сейчас активны тулы X, Y, Z»). Это сохраняет cache.
        """
        if allowed_groups is None:
            return self.to_openai_specs()
        names = set(self.names_in_groups(allowed_groups))
        if extra_names:
            names.update(extra_names)
        out: list[dict] = []
        for name in sorted(names):
            t = self._tools.get(name)
            if t is not None:
                out.append(t.to_openai_spec())
        return out

    def _spawn_critic(self, tool: Tool, args, ctx: ToolContext) -> Optional[dict]:
        """Spawn critic sub-agent для проверки proposed action. Best-effort."""
        try:
            from ..subagent import spawn_subagent
        except Exception:
            return None
        if ctx.workspace is None:
            return None
        task = (
            f"You are a CRITIC sub-agent. Evaluate this proposed action:\n\n"
            f"Tool: `{tool.name}` (group={tool.group}, side_effects={tool.side_effects})\n"
            f"Args: {json.dumps(args, ensure_ascii=False)[:1000]}\n\n"
            "Read relevant files in workspace if needed. Decide: APPROVE / REJECT.\n"
            "If REJECT — give reasoning and suggested alternative.\n"
            "End with: `idle` and a structured summary including verdict (APPROVE|REJECT) "
            "and reasoning (1-3 sentences)."
        )
        try:
            import os as _os
            old = _os.environ.get("MANUS_IS_CRITIC", "")
            _os.environ["MANUS_IS_CRITIC"] = "1"
            try:
                result = spawn_subagent(
                    parent_workspace_path=ctx.workspace.root,
                    task=task,
                    role="critic",
                    timeout_sec=300,
                    max_iterations=12,
                )
            finally:
                if old:
                    _os.environ["MANUS_IS_CRITIC"] = old
                else:
                    _os.environ.pop("MANUS_IS_CRITIC", None)
        except Exception as e:
            logger.warning("critic spawn failed: %s", e)
            return None

        summary = (result.summary or "").upper()
        reject = "REJECT" in summary and "APPROVE" not in summary[:200]
        return {
            "reject": reject,
            "reasoning": result.summary[:500],
            "sub_id": result.sub_id,
        }

    @staticmethod
    def idempotency_key(name: str, args: dict, session_id: str = "") -> str:
        payload = json.dumps({"n": name, "a": args, "s": session_id}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def call(self, name: str, raw_args: dict | str, ctx: ToolContext,
             idempotency_key: Optional[str] = None,
             active_groups: Optional[list[str]] = None,
             agent_mode: str = "EXEC") -> ToolResult:
        """Полный цикл: validate → execute → wrap errors.

        Если задан active_groups — enforce маскирование: тул из неактивной группы
        вернёт ошибку (мягкое "mask, don't remove" при стабильном prompt prefix).

        Если agent_mode == "PLAN" — только plan_safe или always_available тулы разрешены.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                content=f"ERROR: tool '{name}' not registered. Available: {', '.join(self._tools)}",
                is_error=True,
            )

        # B2: Plan mode enforcement
        if agent_mode == "PLAN" and not (tool.plan_safe or tool.always_available):
            return ToolResult(
                content=(
                    f"ERROR: tool '{name}' is NOT plan_safe — can't run in PLAN mode. "
                    "Use exit_plan_mode to switch to EXEC, or pick a plan-safe tool "
                    "(file_read, file_search, recall, write_journal, todo_*, info_search_web, "
                    "page_fetch, list/activate/deactivate_skill, vault_read/list/search/find/tree, "
                    "exit_plan_mode, idle, message_notify_user)."
                ),
                is_error=True,
                metadata={"plan_mode_block": True, "tool_name": name},
            )

        # Enforcement маски: если active_groups задан и тул не в активных — отказываем.
        if active_groups is not None and not tool.always_available:
            if tool.group not in set(active_groups):
                return ToolResult(
                    content=(
                        f"ERROR: tool '{name}' (group '{tool.group}') is NOT active in current phase. "
                        f"Active groups: {active_groups}. "
                        f"Either pick a tool from an active group, or call message_ask_user "
                        f"to request phase switch from the user."
                    ),
                    is_error=True,
                    metadata={"masked": True, "tool_group": tool.group,
                              "active_groups": active_groups},
                )

        # B3: Critic gate — для tools с requires_critic spawn critic subagent
        if tool.requires_critic:
            from os import environ as _env
            critic_mode = _env.get("MANUS_CRITIC_MODE", "loose")
            is_in_critic = _env.get("MANUS_IS_CRITIC", "") in ("1", "true", "yes")
            if critic_mode != "off" and not is_in_critic:
                verdict = self._spawn_critic(tool, raw_args, ctx)
                if verdict and verdict.get("reject"):
                    return ToolResult(
                        content=(
                            f"ERROR: critic REJECTED action `{name}({raw_args})`. "
                            f"Reasoning: {verdict.get('reasoning', '')}"
                        ),
                        is_error=True,
                        metadata={"critic_rejected": True, "verdict": verdict},
                    )

        # Проверка idempotency cache (полезно при resume)
        if idempotency_key and idempotency_key in self._idempotency_cache:
            cached = self._idempotency_cache[idempotency_key]
            logger.info("Idempotency cache HIT for %s (key=%s)", name, idempotency_key)
            return cached

        # Парсим args если пришла строка
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as e:
                return ToolResult(
                    content=f"ERROR: tool arguments are not valid JSON: {e}. "
                            f"Got: {raw_args[:300]!r}. Re-emit the tool call with valid JSON.",
                    is_error=True,
                )

        # Валидация через Pydantic
        if tool.args_schema is not None:
            try:
                args_obj = tool.args_schema(**raw_args)
            except ValidationError as ve:
                return ToolResult(
                    content=f"ERROR: arguments validation failed for tool '{name}'.\n"
                            f"{ve.errors(include_url=False)}\n"
                            f"Schema: {json.dumps(tool.args_schema.model_json_schema(), ensure_ascii=False)[:1000]}",
                    is_error=True,
                )
        else:
            class _Empty(BaseModel):
                pass
            args_obj = _Empty()

        # Execute
        t0 = time.monotonic()
        try:
            result = tool.execute(args_obj, ctx)
        except Exception as e:
            logger.exception("Tool %s raised", name)
            result = ToolResult(content=f"ERROR: tool '{name}' raised {type(e).__name__}: {e}",
                                is_error=True)
        result.duration_ms = int((time.monotonic() - t0) * 1000)

        # Кэшируем idempotent результат если успех
        if idempotency_key and not result.is_error and not tool.side_effects:
            self._idempotency_cache[idempotency_key] = result

        return result
