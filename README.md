# manus-agent

> **Autonomous AI agent in the Manus.im style** — собственный agent loop, иерархический context-compaction, file-as-memory, subprocess sub-agents. Дизайн под длинные задачи (часы итераций) и устойчивость к context-overflow.

Никаких LangChain / LlamaIndex / CrewAI — ~7K LOC своего Python поверх голого OpenAI-compatible API. Работает с любой OpenAI-совместимой моделью; в дефолтной конфигурации — Cloud.ru Foundation Models (Qwen3-Coder-Next, MiniMax-M2, GLM-4.7).

---

## Highlights

| Фича | Где | Зачем |
|---|---|---|
| **5-layer compaction** (snip → microcompact → block-summary → meta-collapse → auto-compact) | [`context.py`](manus/context.py) | Запускать задачи на сотни итераций без OOM, эскалируя только нужный уровень |
| **KV-cache-friendly layout** | `context.py:110` | Стабильный system-prefix → дешёвый prompt cache hit на каждой итерации |
| **Skills 3-tier progressive disclosure** | [`skills_loader.py`](manus/skills_loader.py), [`tools/skills_tool.py`](manus/tools/skills_tool.py) | Tier-1 metadata всегда видно; Tier-2 (~3-5K tok) подгружается на `activate_skill`; Tier-3 ресурсы — lazy через file_read |
| **Tool masking + Plan mode + Critic gate** | [`tools/base.py`](manus/tools/base.py), `agent.py:188` | Soft-mask тулов через `active_groups`; `plan_safe`/`read_only` атрибуты; перед destructive действием — critic-subagent |
| **Async sub-agents (subprocess)** | [`subagent.py`](manus/subagent.py), [`_subagent_runner.py`](manus/_subagent_runner.py) | Crash-isolation; параллельный fan-out research; recursion-guard |
| **File-as-memory** | [`workspace.py`](manus/workspace.py), `agent.py:490` | tool_results > 2000 chars → диск + TL;DR в context + auto-pinned path |
| **Idempotency cache** | `tools/base.py:212` | Одинаковые read-only-вызовы внутри сессии не платят дважды |
| **Stuck detection (OpenHands-style)** | `agent.py:518` | 5 паттернов: same-tool-streak, action-observation repeat, error-repeat, monologue, no-progress-iter |
| **Robust LLM client** | [`llm.py`](manus/llm.py) | tenacity-retry, reasoning_content stripping, truncated tool_calls handling, BadRequest-fallback |
| **Persistent tmux shells** | [`tools/shell.py`](manus/tools/shell.py) | `shell_exec/view/wait/write/kill_*` — каждая session это отдельный tmux, cwd/env переживают вызовы |
| **Secret masking** | `workspace.py:27` | Sanitize AWS / OpenAI sk- / Slack / TG bot / GitHub PAT / PEM при записи в session.jsonl + observations |
| **Phoenix tracing (optional)** | [`observability.py`](manus/observability.py) | `manus run --traces` → локальный Phoenix UI с OpenInference spans |

---

## Quick start

```bash
git clone https://github.com/noesskeetit/manus-agent.git
cd manus-agent

python3 -m venv .venv
.venv/bin/pip install -e .[dev]

# (опционально) Playwright для browser-тулов
.venv/bin/python -m playwright install chromium

# Положи API key — поддерживается ~/.config/manus/secrets.env, ./.env, $PWD/.env
cp .env.example .env
$EDITOR .env  # вставь LLM_API_KEY

# Sanity-check окружения
.venv/bin/manus check

# Запуск
.venv/bin/manus run "сделай deep research по теме X" --model qwen-coder
```

Workspace задачи лежит в `~/manus/workspace/<task_id>/` (todo.md, journal.md, observations/, session.jsonl, state.json).

---

## Architecture

### Agent loop (state machine)

```
┌───────┐
│ INIT  │
└───┬───┘
    ↓
┌───────────┐  LLM    ┌──────────────┐  exec   ┌───────────┐
│ EXECUTING ├────────→│  OBSERVING   ├────────→│ COMPACTING│ (если ctx > 65–92%)
└───────────┘ tools   └──────────────┘ results └─────┬─────┘
    ↑                                                  │
    │              ┌──────────────┐                   │
    └──────────────┤ WAITING_USER │ ←─ ask_user()     │
                   └──────────────┘                   │
                          ↓                            │
                   ┌──────────────┐                   │
                   │  DONE/FAILED │ ←─────────────────┘
                   └──────────────┘
```

Каждая итерация:
1. `_poll_async_subagents()` — собрать результаты завершившихся children
2. `context.assemble()` — собрать messages с sticky-блоком (todo + journal tail + active mask)
3. `context.maybe_compact()` — если оценка токенов превысила threshold
4. `executor.chat(messages, tools)` — single LLM call с tool definitions
5. Выполнить tool calls через registry (idempotency / plan mode / critic gates)
6. Обновить stuck-detector
7. `_save_checkpoint()` — atomic-rename `state.json`

### 5-layer context compaction

Триггерится при превышении соответствующего % от context window модели:

| Этап | Threshold | LLM? | Что делает |
|---|---|---|---|
| 1. SNIP | 65% | нет | Длинные tool_results in-place: `head + … + tail` |
| 2. MICROCOMPACT | 75% | нет | Старые turns (older than 12 turns) → 1-line ремарки эвристикой |
| 3. BLOCK SUMMARY | 80% | да | Chunks по 10 messages → 350-word block summaries (через `summarizer_model`) |
| 4. META-COLLAPSE | 85% | да | Все block-summaries → 1 meta-summary |
| 5. AUTO-COMPACT | 92% | да | Last resort: system + last 5 turns + meta |

Ключевой инвариант: pinned-facts и summaries растут **append-only внутри system-prefix**; sticky-блок (todo + journal + mask) — в конце user-сообщения. Это держит prompt-cache hit стабильным даже после агрессивного compaction.

### Tools

43 тула в 14 группах (после удаления playroom UI):

| Группа | Тулы | Заметка |
|---|---|---|
| `file` | file_read / file_write / file_str_replace / file_list / file_glob | sandbox в workspace |
| `shell` | shell_exec / view / wait / write_to_process / kill_process / kill_session / list_sessions | persistent tmux per session_id |
| `search` | info_search_web (DDG) / page_fetch | DuckDuckGo, без API key |
| `browser` | browser_navigate / click / extract / screenshot / fill / wait_for / close | Playwright (chromium) |
| `message` | message_notify_user / message_ask_user | Telegram → stdin fallback |
| `memory` | recall / read_observation / write_journal | работа с journal/observations |
| `code` | python_exec | CodeAct paradigm — sandboxed Python |
| `deploy` | deploy_expose_port / deploy_apply_deployment | cloudflared tunnel |
| `todo` | todo_add / todo_complete / todo_list / todo_clear | structured todo (Manus-style) |
| `subagent` | spawn_subagent / check_async_subagent / poll_subagents | subprocess fan-out |
| `skills` | list_skills / activate_skill / deactivate_skill | progressive disclosure |
| `lifecycle` | enter_plan_mode / exit_plan_mode / idle | mode + completion signal |
| `vault` (PAC1 only) | vault_tree / list / read / write / search / find / delete / mkdir / move + task_context + task_answer | bitgn harness bindings |

`manus tools` покажет полный реестр текущей сборки.

### Skills (progressive disclosure)

```
manus/skills/
  research/
    SKILL.md           # frontmatter (name, description, triggers) + body
    resources/         # tier-3 lazy
  browsing/
  content/
  pac1/
```

- **Tier-1** (frontmatter — `name + description + triggers`) видно агенту в каждом sticky-блоке
- **Tier-2** (тело SKILL.md) загружается при `activate_skill(name)`. Лимит 3 активных, LRU drop
- **Tier-3** (`resources/*`) — читается lazy через file_read когда понадобится

Активация **расширяет** active_groups объединением, никогда не сужает (важно для multi-skill workflow).

### Sub-agents

Spawn через subprocess (не threading) — crash-isolation, отдельный Python interpreter.

```
parent agent
   └─ spawn_subagent(role="researcher", task="...", async=True)
         └─ subprocess: _subagent_runner.py
               input.json → output.json → poll
```

Specialist roles: `planner`, `executor`, `researcher`, `critic`, `debugger` (промпты в `prompts/roles/`). Recursion-guard ограничен `MANUS_SUBAGENT_RECURSION_DEPTH` (default 2). Async subagents переживают resume парента (полл при перезапуске).

---

## Models

В дефолтной поставке — три провайдера через Cloud.ru FM API (`https://foundation-models.api.cloud.ru/v1`):

| Short name | Model ID | Context | Tool calling | Лучше всего для |
|---|---|---|---|---|
| `qwen-coder` | Qwen/Qwen3-Coder-Next | 256k | ✓ native | **Дефолтный executor** — coding-heavy и tool-use задачи |
| `minimax` | MiniMaxAI/MiniMax-M2 | 192k | ✓ native | Planner для длинных задач (>3ч) |
| `glm` | zai-org/GLM-4.7 | 200k | ✗ (XML thinking) | Compaction / summary (не использовать как executor) |
| `qwen35-vlm` | qwen36-27b-fp8 (vLLM) | 128k | ✓ | Альтернативный executor если FM API ключ ограничен. Требует `MANUS_VLM_BASE` |

Подключить любой свой OpenAI-compatible endpoint можно через `MANUS_CLOUDRU_BASE` (или добавив новую `ModelSpec` в [`config.py`](manus/config.py)).

---

## Configuration

Все настройки — через env vars (читаются из `.env`, `~/.config/manus/secrets.env`, или окружения).

### Required

| Var | Что |
|---|---|
| `LLM_API_KEY` | Ключ Cloud.ru FM API (один на все три cloudru-модели) |

### Optional

| Var | Default | Что |
|---|---|---|
| `MANUS_CLOUDRU_BASE` | `https://foundation-models.api.cloud.ru/v1` | Перенаправить на свой proxy |
| `MANUS_VLM_BASE` | placeholder | URL твоего vLLM-deployment (для `qwen35-vlm`) |
| `MANUS_TG_BOT_TOKEN` | — | Telegram bot token для message_notify_user |
| `MANUS_TG_USER_ID` | — | Telegram user/chat id |
| `BITGN_API_KEY` | — | API ключ для PAC1-бенчмарка |
| `BITGN_HOST` | `https://api.bitgn.com` | Перенаправить bitgn-клиента |

### Behavior toggles (debug/dev)

| Var | Что |
|---|---|
| `MANUS_DISABLE_MASKING=1` | Отключить sanitization секретов в session.jsonl/observations |
| `MANUS_KEEP_TMUX=1` | Не убивать tmux-сессии при выходе |
| `MANUS_KEEP_ASYNC_CHILDREN=1` | Не убивать async sub-agents при atexit |
| `MANUS_CRITIC_MODE` | `off` или `loose` (default `loose`) |
| `MANUS_SUBAGENT_RECURSION_DEPTH` | Default 2 |
| `MANUS_IS_CRITIC=1` | Внутренний флаг — устанавливается subprocess'ом критика, блокирует рекурсию |

См. полный список в [`.env.example`](.env.example).

---

## CLI

```bash
manus run "task" [--model qwen-coder] [--groups file,shell,memory] [--traces]
manus resume <task_id>            # продолжить прерванную сессию
manus status [<task_id>]          # список задач или детали по одной
manus models                      # реестр доступных моделей
manus tools                       # реестр зарегистрированных тулов
manus check                       # sanity-check окружения (API key, deps)
manus pac1 [--benchmark bitgn/pac1-dev] [--limit N] [--no-submit]   # PAC1 runner
```

---

## PAC1 mode (опционально)

Manus умеет запускаться как агент для bitgn PAC1 benchmark — задачи где модель управляет Obsidian-style vault от имени пользователя.

```bash
export BITGN_API_KEY=...
manus pac1 --model qwen-coder --benchmark bitgn/pac1-dev --limit 5
```

Что обёрнуто в [`pac1_runner.py`](manus/pac1_runner.py):
- `BitgnVaultBundle` регистрирует 11 vault-тулов (`vault_tree/list/read/write/search/find/delete/mkdir/move`, `task_context`, `task_answer`) с привязкой к harness URL текущего trial'а
- `system_prompt` = базовый `prompts/system.md` + `PAC1_SYSTEM_EXTRA` (правила vault-conventions, чеклист отказов, anti-loop guard)
- `active_groups = ["vault", "memory", "lifecycle", "communication"]` — жёсткое ограничение
- Между trials автоматически чистится idempotency cache

`bitgn` пакет нужно поставить отдельно (не на pypi) — см. документацию bitgn.

---

## Development

```bash
.venv/bin/pip install -e .[dev]
.venv/bin/pytest                  # все тесты
.venv/bin/pytest -m "not integration"  # только без сетевых
```

### Тесты

| Файл | Что покрывает |
|---|---|
| `tests/test_smoke.py` | Real LLM API smoke-test (требует `LLM_API_KEY`, помечен `@pytest.mark.integration`) |
| `tests/test_e2e_mock.py` | Полный E2E через MockLLM — agent loop, todo, journal, persistence, resume |
| `tests/test_compaction.py` | 5-layer compaction stages — snip, microcompact, block, meta, auto |
| `tests/test_registry.py` | Tool registry — registration, validation, idempotency, plan mode |
| `tests/test_skills_loader.py` | Skills 3-tier disclosure — frontmatter parse, activate, LRU drop |

### Архитектурные принципы (для контрибьюторов)

- **Один процесс — один Agent**. Sub-agents всегда subprocess'ы.
- **Tool result через файл, не через context** — если > 2000 chars, дамп в `observations/`, в context летит TL;DR
- **State persistent после каждой итерации** — atomic rename `state.json`
- **Sticky-блок не растёт** — это всегда compact view (todo + journal tail + mask), без полной истории
- **Compaction идемпотентен** — повторный stage не должен ничего ломать (метки `[snipped]`, `[summary block N]`)

---

## License

MIT — см. [LICENSE](LICENSE).
