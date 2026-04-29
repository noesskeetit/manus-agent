"""Unit-тесты для ToolRegistry — регистрация, валидация, idempotency, plan mode, masking."""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from manus.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


# ---------- Test fixtures: lightweight tools ----------

class _EchoArgs(BaseModel):
    text: str = Field(..., min_length=1)


class EchoTool(Tool):
    name = "echo"
    description = "Echoes input"
    args_schema = _EchoArgs
    group = "test"
    read_only = True
    plan_safe = True

    def execute(self, args, ctx):
        return ToolResult(content=args.text)


class WriteTool(Tool):
    name = "write_thing"
    description = "Mutates external state (mock)"
    args_schema = _EchoArgs
    group = "mut"
    side_effects = True

    def execute(self, args, ctx):
        return ToolResult(content=f"wrote: {args.text}")


class AlwaysTool(Tool):
    name = "noop"
    description = "Always-on tool"
    group = "test"
    always_available = True

    def execute(self, args, ctx):
        return ToolResult(content="ok")


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(EchoTool())
    r.register(WriteTool())
    r.register(AlwaysTool())
    return r


@pytest.fixture
def ctx():
    return ToolContext(workspace=None)


# ---------- Registration ----------

def test_register_duplicate_raises():
    r = ToolRegistry()
    r.register(EchoTool())
    with pytest.raises(ValueError, match="already registered"):
        r.register(EchoTool())


def test_to_openai_specs(registry):
    specs = [t.to_openai_spec() for t in registry._tools.values()]
    assert all(s["type"] == "function" for s in specs)
    names = [s["function"]["name"] for s in specs]
    assert "echo" in names
    assert "write_thing" in names


# ---------- Basic execution ----------

def test_call_unknown_tool_returns_error(registry, ctx):
    result = registry.call("missing", {}, ctx)
    assert result.is_error
    assert "not registered" in result.content


def test_call_validates_args(registry, ctx):
    result = registry.call("echo", {"text": ""}, ctx)
    assert result.is_error
    assert "validation failed" in result.content


def test_call_parses_string_args(registry, ctx):
    result = registry.call("echo", '{"text": "hi"}', ctx)
    assert not result.is_error
    assert result.content == "hi"


def test_call_handles_invalid_json_string(registry, ctx):
    result = registry.call("echo", "{not json", ctx)
    assert result.is_error
    assert "not valid JSON" in result.content


# ---------- Plan mode ----------

def test_plan_mode_allows_plan_safe(registry, ctx):
    result = registry.call("echo", {"text": "hi"}, ctx, agent_mode="PLAN")
    assert not result.is_error


def test_plan_mode_blocks_non_plan_safe(registry, ctx):
    result = registry.call("write_thing", {"text": "x"}, ctx, agent_mode="PLAN")
    assert result.is_error
    assert "plan_safe" in result.content.lower() or "PLAN mode" in result.content


def test_plan_mode_allows_always_available(registry, ctx):
    result = registry.call("noop", {}, ctx, agent_mode="PLAN")
    assert not result.is_error


# ---------- Group masking ----------

def test_active_groups_blocks_inactive_tool(registry, ctx):
    result = registry.call("echo", {"text": "hi"}, ctx, active_groups=["other"])
    assert result.is_error
    assert "NOT active" in result.content


def test_active_groups_allows_active_tool(registry, ctx):
    result = registry.call("echo", {"text": "hi"}, ctx, active_groups=["test"])
    assert not result.is_error


def test_active_groups_always_available_bypasses_mask(registry, ctx):
    result = registry.call("noop", {}, ctx, active_groups=["other"])
    assert not result.is_error


# ---------- Idempotency cache ----------

def test_idempotency_cache_hit(registry, ctx):
    key = ToolRegistry.idempotency_key("echo", {"text": "hi"})
    r1 = registry.call("echo", {"text": "hi"}, ctx, idempotency_key=key)
    r2 = registry.call("echo", {"text": "hi"}, ctx, idempotency_key=key)
    assert r1.content == r2.content
    # Второй вызов должен прийти из кэша — duration_ms у cached result не пересчитывается
    assert id(r1) == id(r2)


def test_idempotency_skips_side_effect_tools(registry, ctx):
    key = ToolRegistry.idempotency_key("write_thing", {"text": "hi"})
    r1 = registry.call("write_thing", {"text": "hi"}, ctx, idempotency_key=key)
    r2 = registry.call("write_thing", {"text": "hi"}, ctx, idempotency_key=key)
    # Side-effect tool НЕ кэшируется — оба вызова реальные
    assert id(r1) != id(r2)


def test_idempotency_skips_errors(registry, ctx):
    key = ToolRegistry.idempotency_key("echo", {"text": ""})
    r1 = registry.call("echo", {"text": ""}, ctx, idempotency_key=key)
    r2 = registry.call("echo", {"text": ""}, ctx, idempotency_key=key)
    assert r1.is_error
    assert r2.is_error
    assert id(r1) != id(r2)  # ошибки не кэшируются


def test_clear_idempotency_cache(registry, ctx):
    key = ToolRegistry.idempotency_key("echo", {"text": "hi"})
    registry.call("echo", {"text": "hi"}, ctx, idempotency_key=key)
    assert key in registry._idempotency_cache
    registry.clear_idempotency_cache()
    assert key not in registry._idempotency_cache


def test_idempotency_key_stable():
    k1 = ToolRegistry.idempotency_key("t", {"a": 1, "b": 2})
    k2 = ToolRegistry.idempotency_key("t", {"b": 2, "a": 1})
    assert k1 == k2  # порядок ключей не важен


def test_idempotency_key_distinct_for_different_args():
    k1 = ToolRegistry.idempotency_key("t", {"a": 1})
    k2 = ToolRegistry.idempotency_key("t", {"a": 2})
    assert k1 != k2
