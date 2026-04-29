"""Main agent loop. State machine: INIT → EXECUTING → OBSERVING → COMPACTING → DONE/FAILED."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import CONFIG
from .context import ContextWindow
from .knowledge import render_hints
from .llm import LLMClient, ToolCall, assistant_message_from_response
from .observability import trace_iteration, trace_tool_call, annotate_span_output
from .tools import ToolRegistry, ToolContext, ToolResult, build_default_registry
from .workspace import Workspace

logger = logging.getLogger("manus.agent")


# ---------- State machine ----------

class AgentPhase(str, Enum):
    INIT = "init"
    EXECUTING = "executing"           # ждёт LLM call
    OBSERVING = "observing"           # выполняет tool calls
    COMPACTING = "compacting"         # сжимает контекст
    WAITING_USER = "waiting_user"     # blocking ask
    DONE = "done"
    FAILED = "failed"


@dataclass
class AgentState:
    """Состояние агента, сохраняется на диск для resume."""
    task_id: str
    task_text: str
    phase: AgentPhase = AgentPhase.INIT
    iteration: int = 0
    consecutive_same_tool: int = 0    # защита от залипания
    last_tool_name: str = ""          # человекочитаемое имя последнего тула (для prompt)
    last_tool_sig: str = ""           # hash(name+args) для сравнения повторов
    no_progress_iter: int = 0         # сколько турнов todo.md не менялся
    last_todo_hash: str = ""
    done: bool = False
    final_summary: str = ""
    failure_reason: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    elapsed_session_seconds: float = 0.0          # накопленное время на этой задаче (для resume)
    active_groups: Optional[list[str]] = None     # None = все группы; иначе — whitelist
    forced_next_tool: Optional[str] = None        # если установлен, на следующем turn принудим вызвать этот тул
    activated_skills: list[str] = field(default_factory=list)   # B1: skills с loaded tier-2
    mode: str = "EXEC"                            # B2: PLAN | EXEC
    async_subagents: list[dict] = field(default_factory=list)  # C1: pending async sub-agents

    # OpenHands-style stuck detector состояние (всё персистится в state.json)
    monologue_count: int = 0                      # сколько подряд assistant без tool_call
    last_error_signature: str = ""                # хэш текста последней tool error
    error_repeat_count: int = 0                   # сколько раз повторилась одна и та же ошибка
    last_action_obs: str = ""                     # signature пары "action+observation"
    action_obs_repeat: int = 0                    # повторов одинаковой пары


# ---------- Agent ----------

class Agent:
    """Главный класс. Создаёт workspace, гоняет loop, управляет всем."""

    def __init__(
        self,
        workspace: Workspace,
        registry: Optional[ToolRegistry] = None,
        executor_model: Optional[str] = None,
        summarizer_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        active_groups: Optional[list[str]] = None,
    ):
        self.workspace = workspace
        self.registry = registry or build_default_registry()
        self.executor = LLMClient(executor_model or CONFIG.executor_model)
        # Summarizer — отдельный клиент, может быть моделью без tool_calling
        try:
            self.summarizer = LLMClient(summarizer_model or CONFIG.summarizer_model)
        except Exception as e:
            logger.warning("Summarizer not available (%s) — using executor for compaction", e)
            self.summarizer = self.executor
        self.system_prompt = system_prompt or self._load_default_system_prompt()
        self.context = ContextWindow(
            model=self.executor.model,
            system_prompt=self.system_prompt,
            sticky_renderer=self._render_sticky,
            summarizer=self.summarizer,
        )
        self.state = AgentState(
            task_id=workspace.task_id, task_text=workspace.task_text,
            active_groups=active_groups,
        )
        self.cancel_flag = threading.Event()
        # Стабильный prefix: ВСЕ specs — KV-cache friendly. Маскирование через system prompt.
        self._all_specs = self.registry.to_openai_specs()

    # ---------- Sticky context (todo + journal tail) ----------

    def _render_sticky(self) -> str:
        parts: list[str] = []
        # Skills tier-1 metadata + active tier-2 (B1)
        try:
            from .skills_loader import discover_skills
            skills = discover_skills()
            if skills:
                active_skills = self.state.activated_skills or []
                lines = ["=== SKILLS (tier-1 metadata) ==="]
                for name, skill in sorted(skills.items()):
                    mark = "[●]" if name in active_skills else "[ ]"
                    lines.append(f"{mark} {skill.metadata.short_metadata}")
                if active_skills:
                    lines.append("")
                    for name in active_skills:
                        if name in skills:
                            instr = skills[name].instructions[:5000]
                            lines.append(f"=== ACTIVE skill `{name}` (tier-2) ===\n{instr}")
                parts.append("\n".join(lines))
        except Exception as e:
            logger.warning("skills inject failed: %s", e)
        # Mode (B2)
        if self.state.mode != "EXEC":
            parts.append(f"=== AGENT MODE: {self.state.mode} ===\n"
                          f"In PLAN mode side-effect tools are blocked. "
                          "Use EnterPlanMode/ExitPlanMode for transitions.")
        if self.workspace.todo.exists():
            todo_txt = self.workspace.todo.read_text(encoding="utf-8")
            if len(todo_txt) > 4000:
                todo_txt = todo_txt[:4000] + "\n... [truncated]"
            parts.append(f"=== todo.md ===\n{todo_txt}")
        # Хвост journal — последние 1500 символов
        if self.workspace.journal.exists():
            j = self.workspace.journal.read_text(encoding="utf-8")
            if j.strip():
                tail = j[-1500:] if len(j) > 1500 else j
                parts.append(f"=== journal.md (tail) ===\n{tail}")
        # Активная маска тулов: показываем модели какие тулы реально доступны.
        # always_available тулы доступны всегда независимо от active_groups.
        active = self._active_tool_names()
        all_groups = self.registry.groups()
        if self.state.active_groups is not None:
            # Список тулов которые ЗАБЛОКИРОВАНЫ (не в active_groups И не always_available)
            blocked_names: list[str] = []
            for grp_name, tool_names in all_groups.items():
                if grp_name in set(self.state.active_groups):
                    continue
                for n in tool_names:
                    t = self.registry.get(n)
                    if t and not t.always_available:
                        blocked_names.append(n)
            mask_block = (
                f"=== ACTIVE tool groups: {', '.join(self.state.active_groups)} ===\n"
                f"Тулы доступные сейчас ({len(active)}): {', '.join(active)}\n"
            )
            if blocked_names:
                mask_block += (
                    f"\nBLOCKED tools (call → error): {', '.join(blocked_names)}.\n"
                    "Это soft mask: tools специфицированы в API, но registry откажет в выполнении."
                )
            parts.append(mask_block)
        else:
            parts.append(f"=== available tools ({len(active)}) ===\n" + ", ".join(active))
        return "\n\n".join(parts)

    def _active_tool_names(self) -> list[str]:
        """Имена реально активных тулов (по active_groups + always_available)."""
        if self.state.active_groups is None:
            return sorted(self.registry.names())
        return self.registry.names_in_groups(self.state.active_groups)

    @staticmethod
    def _load_default_system_prompt() -> str:
        path = Path(__file__).parent / "prompts" / "system.md"
        return path.read_text(encoding="utf-8") if path.exists() else "You are Manus Cloud, an autonomous AI agent."

    # ---------- Управление маской тулов ----------

    def set_active_groups(self, groups: Optional[list[str]]) -> None:
        """Установить активные группы тулов. None = все.

        Это soft mask: prompt prefix не меняется (tools= те же), но registry
        отказывает на вызовах из неактивных групп, и sticky-блок объявляет
        активный subset модели. KV-cache hit сохраняется.
        """
        if groups is not None:
            available = set(self.registry.groups().keys())
            unknown = [g for g in groups if g not in available]
            if unknown:
                raise ValueError(f"Unknown tool groups: {unknown}. Available: {sorted(available)}")
        self.state.active_groups = groups
        logger.info("Active groups set to: %s", groups or "all")

    def force_next_tool(self, tool_name: str) -> None:
        """На следующей итерации заставить модель вызвать конкретный тул через tool_choice."""
        if tool_name not in self.registry.names():
            raise ValueError(f"Unknown tool: {tool_name}")
        self.state.forced_next_tool = tool_name
        logger.info("Will force tool on next iteration: %s", tool_name)

    # ---------- Main loop ----------

    def run(self, max_iterations: Optional[int] = None) -> AgentState:
        max_iter = max_iterations or CONFIG.max_iterations
        # Стартовое сообщение пользователя — задача + knowledge hints
        if not self.context.messages:
            hints = render_hints(self.workspace.task_text)
            hints_block = f"\n\n{hints}\n" if hints else ""
            self.context.add_user(
                f"Задача: {self.workspace.task_text}\n\n"
                f"workspace: {self.workspace.root}\n"
                f"task_id: {self.workspace.task_id}\n"
                f"{hints_block}"
                "Начни: декомпозируй задачу, обнови todo.md, начинай работу. "
                "По завершении — summary.md и idle()."
            )
            # Pin task_text — должен пережить любую compaction
            self.context.auto_pin(f"original task: {self.workspace.task_text[:300]}")
            self.context.auto_pin(f"workspace: {self.workspace.root}")
        self.state.phase = AgentPhase.EXECUTING
        # Засекаем стартовую точку текущей сессии. elapsed_session_seconds — accumulator
        # из предыдущих run'ов (если resume), сюда добавляем (now - session_start_local).
        session_start_local = time.monotonic()

        try:
            while not self.state.done and self.state.iteration < max_iter:
                # 0a. User interrupt — файл CANCEL в workspace
                cancel_file = self.workspace.root / "CANCEL"
                if cancel_file.exists() or self.cancel_flag.is_set():
                    self.state.phase = AgentPhase.FAILED
                    self.state.failure_reason = "cancelled by user (CANCEL file or cancel_flag)"
                    logger.info("Cancellation detected → stopping")
                    break

                # 0b. Cost / time ceiling
                total_tokens = self.state.total_prompt_tokens + self.state.total_completion_tokens
                if total_tokens > CONFIG.max_total_tokens:
                    self.state.phase = AgentPhase.FAILED
                    self.state.failure_reason = f"token ceiling exceeded ({total_tokens} > {CONFIG.max_total_tokens})"
                    logger.warning(self.state.failure_reason)
                    break
                # Накопленное время с учётом всех предыдущих resume'ов
                elapsed_total = self.state.elapsed_session_seconds + (time.monotonic() - session_start_local)
                if elapsed_total > CONFIG.max_session_seconds:
                    self.state.phase = AgentPhase.FAILED
                    self.state.failure_reason = (
                        f"session time exceeded ({elapsed_total:.0f}s > {CONFIG.max_session_seconds}s)"
                    )
                    logger.warning(self.state.failure_reason)
                    break

                self.state.iteration += 1
                with trace_iteration(self.state.task_id, self.state.iteration):
                    self._iteration()
                # Обновляем elapsed перед checkpoint'ом — чтобы resume помнил суммарное время
                self.state.elapsed_session_seconds = (
                    self.state.elapsed_session_seconds
                    + (time.monotonic() - session_start_local)
                )
                # И обнуляем local таймер чтобы не двойного учёта
                session_start_local = time.monotonic()
                self._save_checkpoint()

            if not self.state.done and self.state.iteration >= max_iter:
                logger.warning("Reached max_iterations=%d without done", max_iter)
                self.state.phase = AgentPhase.FAILED
                self.state.failure_reason = "max_iterations"
        except KeyboardInterrupt:
            logger.warning("KeyboardInterrupt — saving state")
            self.state.phase = AgentPhase.FAILED
            self.state.failure_reason = "keyboard_interrupt"
            self._save_checkpoint()
            raise
        except Exception as e:
            logger.exception("Agent loop crashed: %s", e)
            self.state.phase = AgentPhase.FAILED
            self.state.failure_reason = f"crash: {type(e).__name__}: {e}"
            self._save_checkpoint()
            raise

        if self.state.done:
            self.state.phase = AgentPhase.DONE
        self._save_checkpoint()
        return self.state

    def _poll_async_subagents(self) -> None:
        """C1: Проверить завершившиеся async sub-agents и инжектировать их результаты."""
        if not self.state.async_subagents:
            return
        from .subagent import check_async_subagent
        still_pending: list[dict] = []
        for info in self.state.async_subagents:
            result = check_async_subagent(info)
            if result is None:
                still_pending.append(info)
                continue
            # Инжектируем результат как user message
            self.context.add_user(
                f"[BG sub_id={result.sub_id} status={result.status} "
                f"duration={result.duration_sec:.0f}s]\n"
                f"{result.summary[:2000]}"
            )
            self.workspace.append_session({
                "iter": self.state.iteration, "type": "async_subagent_done",
                "sub_id": result.sub_id, "status": result.status,
                "duration": result.duration_sec,
            })
            logger.info("async sub-agent %s completed: %s",
                        result.sub_id, result.status)
        self.state.async_subagents = still_pending

    def _iteration(self) -> None:
        # 0a. Poll async sub-agents (C1)
        self._poll_async_subagents()

        # 0. Compact если нужно
        msgs, est = self.context.assemble()
        if est >= self.context.compact_threshold:
            logger.info("Triggering compaction at %d tokens (threshold %d)", est, self.context.compact_threshold)
            self.state.phase = AgentPhase.COMPACTING
            self.context.maybe_compact()
            msgs, est = self.context.assemble()

        # 1. LLM call
        self.state.phase = AgentPhase.EXECUTING
        logger.info("--- Iter %d --- ctx=%d tok, msgs=%d, summaries=%d, active=%s",
                    self.state.iteration, est, len(self.context.messages),
                    len(self.context.summaries),
                    self.state.active_groups or "all")

        # tool_choice: forced — если задан, иначе auto
        if self.state.forced_next_tool:
            tool_choice: dict | str = {
                "type": "function",
                "function": {"name": self.state.forced_next_tool},
            }
            forced = self.state.forced_next_tool
            self.state.forced_next_tool = None  # сжигаем после использования
            logger.info("Forcing next tool: %s", forced)
        else:
            tool_choice = "auto"

        try:
            resp = self.executor.chat(
                messages=msgs,
                tools=self._all_specs,            # Stable prefix — все specs всегда (KV-cache friendly)
                tool_choice=tool_choice,
            )
            self._llm_error_streak = 0
        except Exception as e:
            self._llm_error_streak = getattr(self, "_llm_error_streak", 0) + 1
            logger.exception("LLM call failed (streak=%d): %s", self._llm_error_streak, e)
            self.workspace.append_session({
                "iter": self.state.iteration, "type": "llm_error",
                "error": f"{type(e).__name__}: {e}",
                "streak": self._llm_error_streak,
            })
            if self._llm_error_streak >= 5:
                logger.error("LLM error streak %d → giving up", self._llm_error_streak)
                self.state.phase = AgentPhase.FAILED
                self.state.failure_reason = f"5 consecutive LLM errors: {type(e).__name__}"
                self.state.done = True  # break loop
                return
            time.sleep(2 ** min(self._llm_error_streak, 4))  # exp backoff up to 16s
            return

        self.state.total_prompt_tokens += resp.prompt_tokens
        self.state.total_completion_tokens += resp.completion_tokens

        # 2. Записываем assistant message
        asst_msg = assistant_message_from_response(resp)
        self.context.add_assistant(asst_msg)
        self.workspace.append_session({
            "iter": self.state.iteration, "type": "assistant",
            "content_preview": (resp.content or "")[:400],
            "tool_calls": [{"id": tc.id, "name": tc.name,
                            "args_preview": (tc.raw_arguments or "")[:300]}
                           for tc in resp.tool_calls],
            "finish_reason": resp.finish_reason,
            "tokens": {"prompt": resp.prompt_tokens, "completion": resp.completion_tokens},
        })

        # 3. Если tool calls есть — выполнить
        if not resp.tool_calls:
            # Нет tool call → монолог (плохой паттерн в agent loop)
            self.state.monologue_count += 1
            if resp.content.strip():
                logger.warning("Plain-text response without tool_call (monologue #%d): %r",
                               self.state.monologue_count, resp.content[:200])
                if self.state.monologue_count >= CONFIG.stuck_monologue_threshold:
                    # Форсим idle если выглядит как готовое
                    looks_done = any(s in resp.content.lower() for s in
                                     ["готово", "выполнено", "finished", "done", "завершено"])
                    if looks_done:
                        logger.info("Monologue suggests completion — forcing idle on next iter")
                        self.force_next_tool("idle")
                    else:
                        self.context.add_user(
                            f"(system) MONOLOGUE STREAK = {self.state.monologue_count}. "
                            "You MUST call a tool. Pick one or call `idle` with summary if task is done."
                        )
                else:
                    self.context.add_user(
                        "(system) Ты ответил без tool call. Если задача закончена — вызови `idle` с summary. "
                        "Если нет — продолжай с tool call."
                    )
                return
            # Совсем пусто — критично, повторяем
            self.context.add_user("(system) Empty response. Continue with tool call.")
            return
        # Сбрасываем monologue counter раз есть tool calls
        self.state.monologue_count = 0

        # 4. Выполнить все tool calls
        self.state.phase = AgentPhase.OBSERVING
        ctx = ToolContext(workspace=self.workspace, agent_state=self.state, cancel_flag=self.cancel_flag)
        for tc in resp.tool_calls:
            if tc.truncated:
                # Стрим обрезался → возвращаем tool_result с просьбой переотправить
                self.context.add_tool_result(
                    tc.id,
                    f"ERROR: tool_call arguments were truncated by stream "
                    f"(got {len(tc.raw_arguments)} chars, invalid JSON). "
                    "Re-emit the call with valid JSON. Don't continue partial.",
                )
                self.workspace.append_session({"iter": self.state.iteration, "type": "tool_truncated",
                                                "name": tc.name})
                continue

            self._track_loop_detection(tc)
            idem_key = self.registry.idempotency_key(tc.name, tc.arguments, self.state.task_id)
            t0 = time.monotonic()
            with trace_tool_call(tc.name, tc.arguments,
                                  task_id=self.state.task_id,
                                  iteration=self.state.iteration) as span:
                result = self.registry.call(
                    tc.name, tc.arguments, ctx,
                    idempotency_key=idem_key,
                    active_groups=self.state.active_groups,
                    agent_mode=self.state.mode,
                )
                annotate_span_output(span, result.content[:8000] if result.content else "",
                                     is_error=result.is_error)
            dur_ms = int((time.monotonic() - t0) * 1000)
            self._track_error_repeat(tc, result)
            self._track_action_obs(tc, result)

            # Большие results дампим на диск
            content = self._maybe_dump_observation(result, tc.name, tc_id=tc.id)

            self.context.add_tool_result(tc.id, content)
            self.workspace.append_session({
                "iter": self.state.iteration, "type": "tool_result",
                "name": tc.name, "tool_call_id": tc.id,
                "is_error": result.is_error,
                "duration_ms": dur_ms,
                "content_preview": content[:400],
                "artifacts": result.artifacts,
                "metadata": result.metadata,
            })

            if self.state.done:
                # idle tool вернул done=True
                break

        # 5. Защита от залипания
        if self.state.consecutive_same_tool > 4:
            self.context.add_user(
                f"(system) You've called `{self.state.last_tool_name}` "
                f"{self.state.consecutive_same_tool} times in a row. "
                "Step back: read todo.md, decide if strategy needs to change. "
                "Use write_journal to reflect, then move on."
            )
            self.state.consecutive_same_tool = 0

        # 6. Прогресс по todo.md (отслеживаем что меняется)
        self._track_todo_progress()

    # ---------- Helpers ----------

    def _maybe_dump_observation(self, result: ToolResult, tool_name: str,
                                 tc_id: str = "") -> str:
        """Если tool result большой — дампим в observations/, в context — TL;DR + path."""
        body = result.content or ""
        threshold = CONFIG.big_observation_threshold
        if len(body) <= threshold:
            return body
        # tc_id (последние 6 символов) добавляем чтобы избежать collision при двух tool_calls
        # одного и того же тула в одной итерации
        suffix = f"-{tc_id[-6:]}" if tc_id else ""
        path = self.workspace.dump_observation(
            name=f"{tool_name}-iter{self.state.iteration:04d}{suffix}",
            content=body,
            turn_id=self.state.iteration,
        )
        head = body[: int(threshold * 0.7)]
        # Auto-pin path так чтобы compaction не съел ссылку
        try:
            rel = str(path.relative_to(self.workspace.root))
            self.context.auto_pin(f"observation: {rel} ({len(body)} chars, tool={tool_name})")
        except Exception:
            pass
        return (
            f"[Large output saved to {path} — {len(body)} chars total]\n"
            f"--- HEAD ({int(threshold * 0.7)} chars) ---\n{head}\n"
            f"... [use read_observation('{path.relative_to(self.workspace.root)}') for full]"
        )

    def _track_loop_detection(self, tc: ToolCall) -> None:
        """Считаем повтор только когда совпадают и имя, и аргументы.
        Имя храним отдельно (last_tool_name) для читаемых prompt'ов модели.
        """
        import hashlib
        sig = hashlib.sha1(
            f"{tc.name}|{json.dumps(tc.arguments, sort_keys=True, default=str)}".encode("utf-8")
        ).hexdigest()[:12]
        if sig == self.state.last_tool_sig:
            self.state.consecutive_same_tool += 1
        else:
            self.state.consecutive_same_tool = 1
            self.state.last_tool_sig = sig
        self.state.last_tool_name = tc.name  # всегда обновляем — для display

    def _track_error_repeat(self, tc: ToolCall, result: ToolResult) -> None:
        """Отслеживаем повторы одной и той же ошибки от одного и того же тула."""
        if not result.is_error:
            self.state.error_repeat_count = 0
            self.state.last_error_signature = ""
            return
        # Сигнатура ошибки = tool_name + первые 200 символов content
        sig = f"{tc.name}::{(result.content or '')[:200]}"
        if sig == self.state.last_error_signature:
            self.state.error_repeat_count += 1
        else:
            self.state.error_repeat_count = 1
            self.state.last_error_signature = sig
        if self.state.error_repeat_count >= CONFIG.stuck_action_error_threshold:
            self.context.add_user(
                f"(system) STUCK: tool `{tc.name}` failed with the same error "
                f"{self.state.error_repeat_count} times. "
                "Stop retrying — read the error, change the strategy, or call message_ask_user."
            )
            self.state.error_repeat_count = 0  # сжигаем чтобы не спамить

    def _track_action_obs(self, tc: ToolCall, result: ToolResult) -> None:
        """OpenHands pattern: ловим повторы action+observation пары."""
        # Hash аргументов + первые символы output
        import hashlib
        sig_src = f"{tc.name}|{json.dumps(tc.arguments, sort_keys=True)[:300]}|{(result.content or '')[:300]}"
        sig = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:12]
        if sig == self.state.last_action_obs:
            self.state.action_obs_repeat += 1
        else:
            self.state.action_obs_repeat = 1
            self.state.last_action_obs = sig
        if self.state.action_obs_repeat >= CONFIG.stuck_action_observation_threshold:
            self.context.add_user(
                f"(system) STUCK: same action+observation pair seen "
                f"{self.state.action_obs_repeat} times in a row. "
                "You're in a loop — break out: review todo.md, try a different approach, "
                "or call message_ask_user for guidance."
            )
            self.state.action_obs_repeat = 0

    def _track_todo_progress(self) -> None:
        try:
            txt = self.workspace.todo.read_text(encoding="utf-8") if self.workspace.todo.exists() else ""
        except Exception:
            txt = ""
        import hashlib
        h = hashlib.sha1(txt.encode()).hexdigest()
        if h == self.state.last_todo_hash:
            self.state.no_progress_iter += 1
        else:
            self.state.no_progress_iter = 0
            self.state.last_todo_hash = h
        if self.state.no_progress_iter > 15:
            logger.warning("todo.md unchanged for %d iterations — pinging agent",
                           self.state.no_progress_iter)
            self.context.add_user(
                f"(system) todo.md hasn't changed in {self.state.no_progress_iter} iterations. "
                "Are you stuck or making meta-progress without updating the plan? "
                "Update todo.md with current state, or escalate via message_ask_user."
            )
            self.state.no_progress_iter = 0

    def _save_checkpoint(self) -> None:
        state_dict = {
            "task_id": self.state.task_id,
            "task_text": self.state.task_text,
            "phase": self.state.phase.value,
            "iteration": self.state.iteration,
            "done": self.state.done,
            "final_summary": self.state.final_summary,
            "failure_reason": self.state.failure_reason,
            "started_at": self.state.started_at,
            "active_groups": self.state.active_groups,
            "activated_skills": self.state.activated_skills,
            "mode": self.state.mode,
            "async_subagents": self.state.async_subagents,
            "elapsed_session_seconds": self.state.elapsed_session_seconds,
            "tokens": {
                "prompt": self.state.total_prompt_tokens,
                "completion": self.state.total_completion_tokens,
            },
            # Stuck detector state — нужен на resume чтобы не забыть стрики
            "stuck": {
                "consecutive_same_tool": self.state.consecutive_same_tool,
                "last_tool_name": self.state.last_tool_name,
                "last_tool_sig": self.state.last_tool_sig,
                "no_progress_iter": self.state.no_progress_iter,
                "last_todo_hash": self.state.last_todo_hash,
                "monologue_count": self.state.monologue_count,
                "last_error_signature": self.state.last_error_signature,
                "error_repeat_count": self.state.error_repeat_count,
                "last_action_obs": self.state.last_action_obs,
                "action_obs_repeat": self.state.action_obs_repeat,
                "forced_next_tool": self.state.forced_next_tool,
            },
            "context": self.context.to_dict(),
        }
        try:
            self.workspace.save_state(state_dict)
        except Exception as e:
            logger.exception("Checkpoint save failed: %s", e)

    # ---------- Resume ----------

    @classmethod
    def resume(cls, task_id: str) -> "Agent":
        ws = Workspace.load(task_id)
        agent = cls(workspace=ws)
        state = ws.load_state()
        if state:
            agent.state.iteration = state.get("iteration", 0)
            agent.state.done = state.get("done", False)
            agent.state.final_summary = state.get("final_summary", "")
            agent.state.failure_reason = state.get("failure_reason", "")
            agent.state.started_at = state.get("started_at", agent.state.started_at)
            agent.state.active_groups = state.get("active_groups")
            agent.state.activated_skills = state.get("activated_skills", []) or []
            agent.state.mode = state.get("mode", "EXEC") or "EXEC"
            agent.state.async_subagents = state.get("async_subagents", []) or []
            agent.state.elapsed_session_seconds = state.get("elapsed_session_seconds", 0.0)
            tokens = state.get("tokens", {})
            agent.state.total_prompt_tokens = tokens.get("prompt", 0)
            agent.state.total_completion_tokens = tokens.get("completion", 0)
            stuck = state.get("stuck", {})
            agent.state.consecutive_same_tool = stuck.get("consecutive_same_tool", 0)
            agent.state.last_tool_name = stuck.get("last_tool_name", "")
            agent.state.last_tool_sig = stuck.get("last_tool_sig", "")
            agent.state.no_progress_iter = stuck.get("no_progress_iter", 0)
            agent.state.last_todo_hash = stuck.get("last_todo_hash", "")
            agent.state.monologue_count = stuck.get("monologue_count", 0)
            agent.state.last_error_signature = stuck.get("last_error_signature", "")
            agent.state.error_repeat_count = stuck.get("error_repeat_count", 0)
            agent.state.last_action_obs = stuck.get("last_action_obs", "")
            agent.state.action_obs_repeat = stuck.get("action_obs_repeat", 0)
            agent.state.forced_next_tool = stuck.get("forced_next_tool")
            agent.context.load_dict(state.get("context", {}))
        return agent
