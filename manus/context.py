"""Context management: tokenизация, обрезание больших tool results, hierarchical compaction.

Стратегия (по советам agent-memory-expert):
- Compaction trigger: 70% от model.context_window
- Target after compaction: 30-40% (запас на новые turns)
- Recent turns raw: последние 8-12 turn'ов всегда в полном виде
- Что НЕ сжимать: system, sticky (todo state), последние 3 tool results, last_error, pinned facts
- Hierarchical map-reduce: блоки по 5-10 turns → block summaries → meta-summary
- Cache-friendly: префикс (system + tools + KB pointers) фиксирован между turns
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import tiktoken

from .config import CONFIG, ModelSpec
from .llm import LLMClient

logger = logging.getLogger("manus.context")


# ---------- Токенайзер ----------

_ENC = None


def _get_enc():
    global _ENC
    if _ENC is None:
        try:
            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENC = tiktoken.get_encoding("o200k_base")
    return _ENC


def count_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        return len(_get_enc().encode(text, disallowed_special=()))
    except Exception:
        # Fallback: ~4 chars per token
        return max(1, len(text) // 4)


def count_message_tokens(msg: dict) -> int:
    """Грубая оценка токенов одного сообщения."""
    total = 4  # overhead per message
    for k, v in msg.items():
        if isinstance(v, str):
            total += count_tokens(v)
        elif isinstance(v, list):
            # tool_calls
            for item in v:
                total += count_tokens(json.dumps(item, ensure_ascii=False))
        elif isinstance(v, dict):
            total += count_tokens(json.dumps(v, ensure_ascii=False))
    return total


# ---------- ContextWindow ----------

@dataclass
class ContextWindow:
    """Управляет messages-листом для LLM с автокомпактификацией."""
    model: ModelSpec
    system_prompt: str
    sticky_renderer: Optional[Any] = None  # callable() -> str — рендерит todo state перед каждым turn'ом
    summarizer: Optional[LLMClient] = None  # GLM для compaction (без tool_calling)

    # Состояние
    messages: list[dict] = field(default_factory=list)   # дельта turns (assistant/tool/user)
    summaries: list[str] = field(default_factory=list)   # накопленные summary блоков
    pinned_facts: list[str] = field(default_factory=list)  # критичные факты (URLs, IDs)

    @property
    def max_ctx(self) -> int:
        return self.model.context_window

    @property
    def compact_threshold(self) -> int:
        return int(self.max_ctx * CONFIG.max_context_pct)

    @property
    def target_after_compact(self) -> int:
        # Хотим оставить запас для новых turns
        return int(self.max_ctx * 0.4)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, msg: dict) -> None:
        """Сохраняет assistant message (с возможными tool_calls). Вне зависимости от формата."""
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def pin_fact(self, fact: str) -> None:
        if fact not in self.pinned_facts:
            self.pinned_facts.append(fact)

    # ---------- Сборка финального prompt'а ----------

    def assemble(self) -> tuple[list[dict], int]:
        """Собирает final messages list для LLM. Возвращает (messages, est_tokens).

        KV-cache friendly layout (по Manus context engineering lessons):
            [system: ТОЛЬКО static prompt + pinned facts + summaries] ← stable prefix, cache hit
            [user: task] [assistant+tools] [tool] [assistant+tools] [tool] ... ← stable history
            [system_ephemeral: current sticky state] ← dynamic, ставится в КОНЦЕ для минимизации cache miss

        pinned_facts и summaries растут редко и слайсами → они в начальном system всё равно
        достаточно стабильны (растут append-only). А sticky (todo+journal) меняется каждый turn.
        """
        # 1. Stable system prefix
        sys_parts: list[str] = [self.system_prompt]
        if self.pinned_facts:
            sys_parts.append("\n\n# === Pinned facts — never forget ===\n\n"
                             + "\n".join(f"- {f}" for f in self.pinned_facts))
        for i, summary in enumerate(self.summaries):
            sys_parts.append(f"\n\n# === History summary block {i+1} ===\n\n{summary}")

        out: list[dict] = [{"role": "system", "content": "".join(sys_parts)}]
        # 2. Stable history
        out.extend(self.messages)

        # 3. Dynamic sticky (todo + journal tail + active mask) — в конец как user-message,
        #    чтобы не ломать KV-cache на длинной истории.
        if self.sticky_renderer:
            try:
                sticky = self.sticky_renderer()
            except Exception as e:
                logger.warning("sticky_renderer failed: %s", e)
                sticky = ""
            if sticky:
                out.append({"role": "user", "content": f"[STATE SNAPSHOT]\n\n{sticky}"})

        total = sum(count_message_tokens(m) for m in out)
        return out, total

    # ---------- Compaction (5-layer pipeline по Anthropic Claude Code) ----------

    def maybe_compact(self) -> bool:
        """5-layer compaction: snip → microcompact → block-summary → meta-collapse → auto-compact.

        Каждый stage triggers по своему % threshold. Эскалация: если после stage всё ещё
        больше threshold следующего — применяем следующий stage.

        Возвращает True если хоть один stage отработал.
        """
        msgs, est = self.assemble()
        max_ctx = self.max_ctx
        any_compacted = False

        # Stage 1: SNIP — head+tail на длинных tool_results (no LLM, cheap)
        if est >= int(max_ctx * CONFIG.snip_pct):
            if self._stage_snip():
                any_compacted = True
                _, est = self.assemble()
                logger.info("[compact stage=snip] → %d tok", est)

        # Stage 2: MICROCOMPACT — старые turns (раньше last 16) → 1 строка LLM
        if est >= int(max_ctx * CONFIG.microcompact_pct):
            if self._stage_microcompact():
                any_compacted = True
                _, est = self.assemble()
                logger.info("[compact stage=microcompact] → %d tok", est)

        # Stage 3: BLOCK SUMMARY — chunks по 10 → 350-словные block summaries
        if est >= int(max_ctx * CONFIG.block_summary_pct):
            if self._stage_block_summary():
                any_compacted = True
                _, est = self.assemble()
                logger.info("[compact stage=block_summary] → %d tok", est)

        # Stage 4: META-COLLAPSE — block summaries → 1 meta-summary
        if est >= int(max_ctx * CONFIG.meta_collapse_pct):
            if self._stage_meta_collapse():
                any_compacted = True
                _, est = self.assemble()
                logger.info("[compact stage=meta_collapse] → %d tok", est)

        # Stage 5: AUTO-COMPACT — last resort, оставляем system + last 5 turns + meta
        if est >= int(max_ctx * CONFIG.auto_compact_pct):
            if self._stage_auto_compact():
                any_compacted = True
                _, est = self.assemble()
                logger.warning("[compact stage=auto_compact LAST RESORT] → %d tok", est)

        return any_compacted

    # ---- Stage 1: SNIP ----

    def _stage_snip(self) -> bool:
        """Head+tail на длинных tool_results in-place. Без LLM."""
        if not self.messages:
            return False
        head_n = CONFIG.snip_keep_head
        tail_n = CONFIG.snip_keep_tail
        min_size = CONFIG.snip_min_size
        snipped = 0
        for m in self.messages:
            if m.get("role") != "tool":
                continue
            content = m.get("content", "") or ""
            if len(content) < min_size:
                continue
            if "[snipped" in content:  # already snipped
                continue
            head = content[:head_n]
            tail = content[-tail_n:]
            removed = len(content) - head_n - tail_n
            m["content"] = (
                f"{head}\n\n...[snipped {removed} chars in middle]...\n\n{tail}"
            )
            snipped += 1
        return snipped > 0

    # ---- Stage 2: MICROCOMPACT ----

    def _stage_microcompact(self) -> bool:
        """Сжать старые turns (раньше last 16) в 1-строчные summaries.
        Используем local heuristic вместо LLM (cheap), сохраняя tool_call_id chains.
        """
        keep_n = CONFIG.keep_last_turns_raw + 4  # держим больше для microcompact
        if len(self.messages) <= keep_n:
            return False
        split = len(self.messages) - keep_n
        while split < len(self.messages) and self.messages[split].get("role") == "tool":
            split += 1
        if split >= len(self.messages):
            return False
        old = self.messages[:split]
        if not old:
            return False
        # Пары assistant+tool сжимаем в одну строку user-style ремарки
        compact_lines: list[str] = []
        i = 0
        while i < len(old):
            m = old[i]
            role = m.get("role", "?")
            if role == "assistant" and m.get("tool_calls"):
                tcs = m.get("tool_calls", [])
                names = [tc["function"]["name"] for tc in tcs if "function" in tc]
                # Подбираем последующие tool_results
                tool_results: list[str] = []
                j = i + 1
                while j < len(old) and old[j].get("role") == "tool":
                    tr = old[j].get("content", "") or ""
                    summary = tr[:120].replace("\n", " ")
                    if "ERROR" in tr[:200]:
                        summary = "ERROR: " + summary
                    tool_results.append(summary)
                    j += 1
                line = f"[old turn] {','.join(names)} → {' | '.join(tool_results)[:240]}"
                compact_lines.append(line)
                i = j
            elif role == "user":
                content = (m.get("content") or "")[:200].replace("\n", " ")
                compact_lines.append(f"[old user] {content}")
                i += 1
            else:
                content = (m.get("content") or "")[:120].replace("\n", " ")
                compact_lines.append(f"[old {role}] {content}")
                i += 1
        # Wrap as единый user message
        compacted_block = {
            "role": "user",
            "content": "[MICRO-COMPACT historical turns]\n" + "\n".join(compact_lines),
        }
        self.messages = [compacted_block] + self.messages[split:]
        return True

    # ---- Stage 3: BLOCK SUMMARY (LLM) ----

    def _stage_block_summary(self) -> bool:
        """LLM-driven blockwise summarization. Текущая логика."""
        if self.summarizer is None:
            return False
        if len(self.messages) < CONFIG.auto_compact_min_turns:
            return False
        keep_n = CONFIG.keep_last_turns_raw
        if len(self.messages) <= keep_n:
            return False
        split = len(self.messages) - keep_n
        while split < len(self.messages) and self.messages[split].get("role") == "tool":
            split += 1
        if split >= len(self.messages):
            return False

        to_compact = self.messages[:split]
        keep_raw = self.messages[split:]
        chunks: list[list[dict]] = []
        cur: list[dict] = []
        for m in to_compact:
            cur.append(m)
            if len(cur) >= 10:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)

        new_summaries: list[str] = []
        for ch in chunks:
            block_text = self._serialize_chunk(ch)
            try:
                summary = self._summarize_block(block_text)
                new_summaries.append(summary)
            except Exception as e:
                logger.exception("block summary failed: %s", e)
                new_summaries.append(f"[Summary failed]\n{block_text[:1500]}\n...[truncated]")

        self.summaries = self.summaries + new_summaries
        self.messages = keep_raw
        return True

    # ---- Stage 4: META-COLLAPSE ----

    def _stage_meta_collapse(self) -> bool:
        """Все block summaries → 1 meta-summary."""
        if self.summarizer is None or len(self.summaries) <= 1:
            return False
        try:
            meta = self._summarize_block(
                "\n\n".join(f"## Block {i+1}\n{s}" for i, s in enumerate(self.summaries)),
                is_meta=True,
            )
            self.summaries = [meta]
            return True
        except Exception as e:
            logger.exception("meta-collapse failed: %s", e)
            return False

    # ---- Stage 5: AUTO-COMPACT (last resort) ----

    def _stage_auto_compact(self) -> bool:
        """Drop everything except system + last 5 turns + 1 meta-summary."""
        if len(self.messages) <= 5:
            return False
        keep = self.messages[-5:]
        # Сдвигаем boundary чтобы не начинать с tool
        while keep and keep[0].get("role") == "tool":
            keep = keep[1:]
        if not keep:
            return False
        # Создаём emergency note
        dropped = len(self.messages) - len(keep)
        emergency = {
            "role": "user",
            "content": (
                f"[AUTO-COMPACT EMERGENCY] Dropped {dropped} earlier messages due to "
                f"context overflow. Pinned facts and summaries preserved. Continue task."
            ),
        }
        self.messages = [emergency] + keep
        return True

    # ---- Auto-pin facts (вызывается из agent.py) ----

    def auto_pin(self, fact: str) -> None:
        """Auto-add fact to pinned (with dedup)."""
        if not fact or len(fact) > 500:
            return
        if fact in self.pinned_facts:
            return
        # Лимит pinned (избегаем infinite growth)
        if len(self.pinned_facts) >= 30:
            self.pinned_facts.pop(0)  # FIFO drop oldest
        self.pinned_facts.append(fact)

    @staticmethod
    def _serialize_chunk(messages: list[dict]) -> str:
        out = []
        for m in messages:
            role = m.get("role", "?")
            if role == "assistant":
                content = m.get("content") or ""
                tcs = m.get("tool_calls") or []
                tc_text = ""
                if tcs:
                    tc_text = "\n  tool_calls: " + ", ".join(
                        f"{tc.get('function', {}).get('name', '?')}({tc.get('function', {}).get('arguments', '')[:200]})"
                        for tc in tcs
                    )
                out.append(f"[assistant] {content[:800]}{tc_text}")
            elif role == "tool":
                content = m.get("content") or ""
                out.append(f"[tool_result id={m.get('tool_call_id', '?')[:8]}] {content[:1200]}")
            elif role == "user":
                out.append(f"[user] {m.get('content', '')[:800]}")
            else:
                out.append(f"[{role}] {json.dumps(m, ensure_ascii=False)[:800]}")
        return "\n".join(out)

    def _summarize_block(self, block_text: str, is_meta: bool = False) -> str:
        if self.summarizer is None:
            raise RuntimeError("No summarizer")
        instruction = (
            "Ты сжимаешь историю работы AI-агента в краткую summary для дальнейшего использования. "
            "Сохрани: цели/решения, key URLs/IDs/paths, ошибки и их фиксы, текущий blockers. "
            "ОПУСТИ: сырые tool args, низкоуровневые pid'ы, повторяющиеся scrolling-actions. "
            f"Размер: ~{'500' if is_meta else '350'} слов, абзацами без буллетов."
            + (" Это meta-summary из уже сжатых блоков, дай высокоуровневый обзор." if is_meta else "")
        )
        resp = self.summarizer.chat(
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": block_text[:30_000]},  # safety
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        return resp.content.strip()

    # ---------- Persistence helpers ----------

    def to_dict(self) -> dict:
        return {
            "messages": self.messages,
            "summaries": self.summaries,
            "pinned_facts": self.pinned_facts,
        }

    def load_dict(self, d: dict) -> None:
        self.messages = d.get("messages", [])
        self.summaries = d.get("summaries", [])
        self.pinned_facts = d.get("pinned_facts", [])


# ---------- Helpers ----------

def truncate_for_context(text: str, max_chars: int = 2000,
                         marker: str = "...[truncated, full at {path}]",
                         path: Optional[str] = None) -> str:
    """Обрезает большой text до max_chars (head + tail), вставляя marker."""
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.75)]
    tail = text[-int(max_chars * 0.2):]
    pmark = marker.format(path=path or "<not saved>")
    return f"{head}\n\n{pmark}\n\n{tail}"
