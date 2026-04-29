"""Shared pytest fixtures."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

# LLMClient требует API key уже на этапе __init__ (валидация),
# даже если в тестах он подменяется на mock. Stub чтобы юнит-тесты были
# независимы от окружения. Integration-тесты сами skipаются без реального ключа.
os.environ.setdefault("LLM_API_KEY", "test-stub")


@dataclass
class FakeModelSpec:
    id: str = "fake-model"
    short: str = "fake"
    api_base: str = "http://localhost/v1"
    api_key_env: str = "FAKE_KEY"
    context_window: int = 4096
    supports_tool_calling: bool = True
    notes: str = ""


@pytest.fixture
def fake_model():
    return FakeModelSpec()


@pytest.fixture
def long_tool_result_content():
    return "X" * 5000  # достаточно длинно чтобы snip сработал
