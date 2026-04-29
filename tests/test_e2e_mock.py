"""End-to-end test с моком LLM. Проверяет полный lifecycle агента:
workspace creation, todo updates, tool execution, idle detection, persistence, resume.

Без реального Cloud.ru API — но через интерфейс, идентичный production.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

# Гарантируем что импорты идут из локального проекта
sys.path.insert(0, str(Path(__file__).parent.parent))

from manus.agent import Agent, AgentPhase
from manus.config import PATHS
from manus.llm import LLMClient, LLMResponse, ToolCall
from manus.workspace import Workspace
from manus.tools import build_default_registry


# ---------- Mock LLM ----------

class MockLLMClient:
    """Прокси над реальным API — возвращает заранее заготовленные ответы по очереди."""

    def __init__(self, scenario: list[dict], short: str = "qwen-coder"):
        from manus.config import get_model
        self.model = get_model(short)
        self.scenario = scenario
        self.idx = 0
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, tool_choice=None,
             temperature=None, max_tokens=None) -> LLMResponse:
        self.calls.append({"messages": len(messages), "tools": len(tools or [])})
        if self.idx >= len(self.scenario):
            # default — idle (если что-то пошло не так)
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="auto-idle", name="idle",
                                     arguments={"summary": "Auto-idle (scenario exhausted)"})],
                finish_reason="tool_calls",
                prompt_tokens=100, completion_tokens=20, model=self.model.id,
                raw_message={"role": "assistant", "content": None},
            )
        step = self.scenario[self.idx]
        self.idx += 1
        # step: {"content": "...", "tools": [{"name": "...", "args": {...}}]}
        tcs = []
        for i, tc in enumerate(step.get("tools", [])):
            tcs.append(ToolCall(
                id=f"tc-{self.idx}-{i}",
                name=tc["name"],
                arguments=tc.get("args", {}),
                raw_arguments=json.dumps(tc.get("args", {}), ensure_ascii=False),
            ))
        return LLMResponse(
            content=step.get("content", ""),
            tool_calls=tcs,
            finish_reason="tool_calls" if tcs else "stop",
            prompt_tokens=step.get("prompt_tokens", 100),
            completion_tokens=step.get("completion_tokens", 50),
            model=self.model.id,
            raw_message={"role": "assistant", "content": step.get("content", None)},
        )


# ---------- Сценарий ----------

SCENARIO = [
    # Step 1: декомпозируем — обновляем todo.md
    {
        "content": "Декомпозирую задачу.",
        "tools": [{
            "name": "file_str_replace",
            "args": {
                "file": "todo.md",
                "old_str": "_Декомпозируй задачу здесь._",
                "new_str": (
                    "- [ ] Шаг 1: создать файл poem.md с коротким стихом про море\n"
                    "- [ ] Шаг 2: записать summary.md и завершить"
                ),
            },
        }],
    },
    # Step 2: пишем стих
    {
        "tools": [{
            "name": "file_write",
            "args": {
                "file": "poem.md",
                "content": (
                    "# Море\n\n"
                    "Ветер солёный шепчет волне,\n"
                    "Чайки кричат о грядущей весне.\n"
                    "В синей дали корабли уплывают,\n"
                    "Память о берегах никогда не теряют.\n"
                ),
            },
        }],
    },
    # Step 3: отмечаем 1 шаг как done
    {
        "tools": [{
            "name": "file_str_replace",
            "args": {
                "file": "todo.md",
                "old_str": "- [ ] Шаг 1: создать файл poem.md с коротким стихом про море",
                "new_str": "- [x] Шаг 1: создан poem.md",
            },
        }],
    },
    # Step 4: записываем journal
    {
        "tools": [{
            "name": "write_journal",
            "args": {"entry": "Стих написан, 4 строки. Tone — спокойный."},
        }],
    },
    # Step 5: пишем summary.md
    {
        "tools": [{
            "name": "file_write",
            "args": {
                "file": "summary.md",
                "content": (
                    "# Итог\n\nЗадача: написать стих про море. Готово.\n\n"
                    "Артефакты:\n- poem.md\n- todo.md\n"
                ),
            },
        }],
    },
    # Step 6: notify (TG fallback в stdout)
    {
        "tools": [{
            "name": "message_notify_user",
            "args": {"text": "[mock] Готово. Стих в poem.md."},
        }],
    },
    # Step 7: idle
    {
        "tools": [{
            "name": "idle",
            "args": {"summary": "Стих про море написан, summary создан."},
        }],
    },
]


def run_e2e():
    PATHS.ensure()
    ws = Workspace.create("Напиши короткий стих про море", task_id="e2e-mock-test")
    # Очистка старого workspace если есть
    for p in [ws.todo, ws.journal, ws.summary, ws.session_log, ws.state_file,
              ws.root / "poem.md"]:
        if p.exists():
            p.unlink()
    if ws.observations_dir.exists():
        shutil.rmtree(ws.observations_dir)
    ws = Workspace.create("Напиши короткий стих про море", task_id="e2e-mock-test")

    agent = Agent(
        workspace=ws,
        registry=build_default_registry(),
    )
    # Подменяем executor + summarizer на mock
    mock = MockLLMClient(SCENARIO)
    agent.executor = mock
    agent.summarizer = mock  # на случай compaction
    agent.context.summarizer = mock

    print(f"Workspace: {ws.root}")
    print(f"Tools: {len(agent._all_specs)}")
    state = agent.run(max_iterations=20)
    print(f"\nFinal phase: {state.phase.value}")
    print(f"Iterations: {state.iteration}")
    print(f"Done: {state.done}")
    print(f"Final summary: {state.final_summary}")
    print(f"\nLLM calls: {len(mock.calls)}")
    return state, ws


import pytest


@pytest.fixture(scope="module")
def e2e_workspace():
    """Запускаем агента один раз на модуль, тестам отдаём готовый workspace."""
    state, ws = run_e2e()
    yield ws


def assert_files(ws: Workspace):
    must_exist = ["poem.md", "summary.md", "todo.md", "journal.md", "session.jsonl", "state.json"]
    for f in must_exist:
        p = ws.root / f
        assert p.exists(), f"Missing file: {p}"
        print(f"  ✓ {f} ({p.stat().st_size} bytes)")
    # Содержимое poem.md
    poem = (ws.root / "poem.md").read_text(encoding="utf-8")
    assert "море" in poem.lower() or "Море" in poem, "poem.md doesn't mention sea"
    # todo.md имеет [x]
    todo = ws.todo.read_text(encoding="utf-8")
    assert "[x]" in todo, "todo.md not updated with [x]"
    # journal.md имеет нашу запись
    journal = ws.journal.read_text(encoding="utf-8")
    assert "Стих написан" in journal, "journal.md missing entry"
    # session.jsonl — несколько записей
    session_lines = ws.session_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(session_lines) >= 6, f"session.jsonl too short: {len(session_lines)} lines"
    # state.json — done=True
    state = json.loads(ws.state_file.read_text(encoding="utf-8"))
    assert state.get("done"), f"state.json done={state.get('done')}"
    assert state.get("phase") == "done"


def test_e2e_artifacts(e2e_workspace):
    """Проверка что после прогона мок-сценария созданы все ожидаемые артефакты."""
    assert_files(e2e_workspace)


def test_resume(e2e_workspace):
    """Проверим что resume работает — поднимаем агента из state, пробуем продолжить (он сразу видит done)."""
    ag2 = Agent.resume(e2e_workspace.task_id)
    assert ag2.state.iteration > 0
    assert ag2.state.done, "Resumed state should preserve done flag"


if __name__ == "__main__":
    print("=== E2E mock test ===\n")
    state, ws = run_e2e()
    print("\n=== Assertions ===")
    try:
        assert_files(ws)
        ag2 = Agent.resume(ws.task_id)
        assert ag2.state.iteration > 0 and ag2.state.done
        print("\nALL PASSED")
        sys.exit(0)
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
