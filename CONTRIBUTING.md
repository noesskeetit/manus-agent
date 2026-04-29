# Contributing

Спасибо что заглянули. Этот проект небольшой и держится без иерархии — pull request'ы приветствуются.

## Setup

```bash
git clone https://github.com/noesskeetit/manus-agent.git
cd manus-agent
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
.venv/bin/python -m playwright install chromium  # для browser-тулов
cp .env.example .env  # вставь LLM_API_KEY
```

## Проверка перед PR

```bash
.venv/bin/pytest -m "not integration"   # unit-тесты, без сети
.venv/bin/manus check                   # sanity окружения
.venv/bin/manus tools                   # реестр тулов цел
```

Если меняешь LLM-related код — желательно прогнать integration:
```bash
.venv/bin/pytest                        # все включая integration (требует LLM_API_KEY)
```

## Что особенно полезно

- **Unit-coverage** для модулей которые сейчас покрыты неполно: `subagent.py`, `todo_tracker.py`, `knowledge.py`, `tools/shell.py`
- **Адаптеры под другие LLM-провайдеры** (Anthropic, Google, OpenRouter — через ModelSpec)
- **Streaming tool_calls** в `llm.py` (сейчас non-streaming)
- **Cross-session learning** — выделять lessons после задачи в постоянную knowledge базу

## Архитектурные принципы

- Один процесс — один `Agent`. Sub-agents всегда subprocess'ы.
- Tool result > 2000 chars никогда не идёт в context целиком — дампится в `observations/`, в context летит TL;DR + path
- State persistent **после каждой итерации** через atomic rename `state.json`
- Compaction stages идемпотентны: повторный прогон того же stage не должен ничего ломать
- Sticky-блок (todo + journal tail + mask) — фиксированного размера, не растёт между итерациями

## Стиль

- Type hints — везде где есть LLM/tool boundaries (`Pydantic` schemas обязательны для tool args)
- Docstrings на русском или английском — без претензий, главное чтобы было понятно
- Никаких эмодзи в логах / вывода CLI без явной просьбы

## Issues

Если нашёл баг — приведи минимальный repro: задача (`manus run "..."`), модель, expected vs actual. Workspace-директория из `~/manus/workspace/<task_id>/` (`state.json` + `session.jsonl`) — золотой источник правды для voiceover'а проблемы.
