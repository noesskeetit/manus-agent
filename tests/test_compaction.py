"""Unit-тесты для context compaction stages.

Покрывает stages 1-2 (без LLM): snip, microcompact.
Stages 3-5 требуют summarizer и тестируются интеграционно через test_e2e_mock.
"""
from __future__ import annotations

import pytest

from manus.context import ContextWindow, count_tokens, truncate_for_context


def _make_ctx(messages=None, model=None):
    """Сделать ContextWindow с минимальными настройками."""
    if model is None:
        from tests.conftest import FakeModelSpec
        model = FakeModelSpec()
    cw = ContextWindow(model=model, system_prompt="SYSTEM PROMPT")
    if messages:
        cw.messages = messages
    return cw


# ---------- Stage 1: SNIP ----------

def test_snip_long_tool_result(fake_model, long_tool_result_content):
    cw = _make_ctx(model=fake_model, messages=[
        {"role": "user", "content": "do thing"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": long_tool_result_content},
    ])
    changed = cw._stage_snip()
    assert changed is True
    snipped = cw.messages[2]["content"]
    assert "[snipped" in snipped
    assert len(snipped) < len(long_tool_result_content)


def test_snip_idempotent(fake_model, long_tool_result_content):
    """Повторный snip не должен ломать уже сжатое содержимое."""
    cw = _make_ctx(model=fake_model, messages=[
        {"role": "tool", "tool_call_id": "c1", "content": long_tool_result_content},
    ])
    cw._stage_snip()
    after_first = cw.messages[0]["content"]
    cw._stage_snip()
    after_second = cw.messages[0]["content"]
    assert after_first == after_second


def test_snip_skips_short_content(fake_model):
    cw = _make_ctx(model=fake_model, messages=[
        {"role": "tool", "tool_call_id": "c1", "content": "short"},
    ])
    changed = cw._stage_snip()
    assert changed is False
    assert cw.messages[0]["content"] == "short"


def test_snip_skips_non_tool_messages(fake_model, long_tool_result_content):
    cw = _make_ctx(model=fake_model, messages=[
        {"role": "user", "content": long_tool_result_content},
    ])
    changed = cw._stage_snip()
    assert changed is False
    # User message нетронут
    assert cw.messages[0]["content"] == long_tool_result_content


# ---------- Stage 2: MICROCOMPACT ----------

def test_microcompact_collapses_old_turns(fake_model):
    """Старые turns (раньше keep_last_turns_raw + 4) должны схлопнуться в одно user-message."""
    msgs = []
    for i in range(40):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "tool_x", "arguments": "{}"}}
        ]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"})
    cw = _make_ctx(model=fake_model, messages=msgs)
    initial_count = len(cw.messages)
    changed = cw._stage_microcompact()
    assert changed is True
    # Должен сильно сократиться
    assert len(cw.messages) < initial_count
    # Первый message — compacted block
    assert cw.messages[0]["role"] == "user"
    assert "MICRO-COMPACT" in cw.messages[0]["content"]


def test_microcompact_skips_short_history(fake_model):
    cw = _make_ctx(model=fake_model, messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    changed = cw._stage_microcompact()
    assert changed is False


# ---------- token counting ----------

def test_count_tokens_returns_positive():
    n = count_tokens("hello world this is a test")
    assert n > 0


def test_count_tokens_empty():
    assert count_tokens("") == 0


# ---------- assemble layout ----------

def test_assemble_pinned_facts_in_system(fake_model):
    cw = _make_ctx(model=fake_model)
    cw.pin_fact("URL: https://example.com")
    cw.add_user("test")
    msgs, tokens = cw.assemble()
    sys_msg = next(m for m in msgs if m["role"] == "system")
    assert "https://example.com" in sys_msg["content"]
    assert tokens > 0


def test_assemble_summaries_in_system(fake_model):
    cw = _make_ctx(model=fake_model)
    cw.summaries = ["Summary block A", "Summary block B"]
    cw.add_user("test")
    msgs, _ = cw.assemble()
    sys_msg = next(m for m in msgs if m["role"] == "system")
    assert "Summary block A" in sys_msg["content"]
    assert "Summary block B" in sys_msg["content"]


def test_pin_fact_dedupes(fake_model):
    cw = _make_ctx(model=fake_model)
    cw.pin_fact("F1")
    cw.pin_fact("F1")
    assert cw.pinned_facts == ["F1"]


# ---------- truncate helper ----------

def test_truncate_for_context_keeps_short_unchanged():
    s = "hello"
    out = truncate_for_context(s, max_chars=100)
    assert out == s


def test_truncate_for_context_cuts_long():
    s = "X" * 5000
    out = truncate_for_context(s, max_chars=200)
    assert len(out) < len(s)
    assert "..." in out or "[truncated" in out


# ---------- to_dict / load_dict round-trip ----------

def test_context_serialize_round_trip(fake_model):
    cw = _make_ctx(model=fake_model)
    cw.pin_fact("F")
    cw.summaries = ["S"]
    cw.add_user("hello")
    d = cw.to_dict()

    cw2 = _make_ctx(model=fake_model)
    cw2.load_dict(d)
    assert cw2.pinned_facts == ["F"]
    assert cw2.summaries == ["S"]
    assert any(m.get("content") == "hello" for m in cw2.messages)
