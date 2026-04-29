"""Smoke-test: проверить что LLM client + tool calling работают на Cloud.ru FM.

Требует LLM_API_KEY. Помечен как integration и по умолчанию скипается в pytest.
"""
from __future__ import annotations

import os
import sys

import pytest

from manus.llm import LLMClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("LLM_API_KEY"), reason="needs LLM_API_KEY"),
]


def test_basic_chat():
    client = LLMClient("qwen-coder")
    resp = client.chat(
        messages=[
            {"role": "system", "content": "You are a helpful assistant. Respond concisely in Russian."},
            {"role": "user", "content": "Скажи одно число от 1 до 10."},
        ],
        max_tokens=50,
    )
    print(f"[basic] content: {resp.content!r}")
    print(f"[basic] tokens: prompt={resp.prompt_tokens}, completion={resp.completion_tokens}")
    assert resp.content
    assert resp.finish_reason in ("stop", "length")


def test_tool_calling():
    client = LLMClient("qwen-coder")
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Получить погоду в городе",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Название города"},
                },
                "required": ["city"],
            },
        },
    }]
    resp = client.chat(
        messages=[
            {"role": "system", "content": "You can call tools. Always use them when relevant."},
            {"role": "user", "content": "Какая погода в Москве?"},
        ],
        tools=tools,
        tool_choice="auto",
        max_tokens=200,
    )
    print(f"[tools] content: {resp.content!r}")
    print(f"[tools] tool_calls: {[(tc.name, tc.arguments) for tc in resp.tool_calls]}")
    print(f"[tools] finish: {resp.finish_reason}")
    assert resp.tool_calls, "Expected at least one tool call"
    tc = resp.tool_calls[0]
    assert tc.name == "get_weather"
    assert "city" in tc.arguments


if __name__ == "__main__":
    print("--- test_basic_chat ---")
    try:
        test_basic_chat()
        print("OK\n")
    except Exception as e:
        print(f"FAIL: {e}\n")
        sys.exit(1)

    print("--- test_tool_calling ---")
    try:
        test_tool_calling()
        print("OK\n")
    except Exception as e:
        print(f"FAIL: {e}\n")
        sys.exit(1)

    print("All smoke tests passed.")
