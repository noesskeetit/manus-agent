---
name: research
description: Deep external info gathering with search + page_fetch + cross-validation
triggers: [research, исследуй, разберись, найди инфу, изучи рынок, обзор, сравни, compare, investigate]
active_groups: [research, file, memory, lifecycle, communication]
allowed_role: researcher
version: 1
---

# Skill: Research

Универсальный паттерн "сбор внешней информации". Применим к нишам, продуктам, темам, локациям, конкурентам.

## Принципы

1. **Источник истины — оригинал, не snippet.** `info_search_web` даёт snippets — это указатели. Реальные факты бери через `page_fetch` или `browser_navigate`.
2. **Cross-validation на ключевых фактах.** Любая цифра, дата, утверждение — минимум 2 источника. Если расходятся — фиксируй разногласие.
3. **Step-by-step search.** "USA capital", "USA first president" — отдельно, не одной строкой.
4. **Несколько entities — separately.**
5. **Сохраняй сырые данные.** Длинные web pages автоматически дампятся в `observations/`. Финальная сводка — твоя работа в `research/<topic>.md`.

## Workflow

1. **Декомпозиция темы → подтемы.**
2. **Параллельный search** на 3-5 запросах (если они независимы). `info_search_web` × 3.
3. **Углубление через `page_fetch`** на топ-3 результата каждого запроса.
4. **Извлечение фактов** в `research/<topic>.md` — структурированный markdown с цитатами и URL.
5. **Cross-validation** — если у двух источников разные цифры, ищи третий.
6. **Сводный обзор** в `research/_overview.md`.

## Шаблон research/<topic>.md

```markdown
# <Topic>

## Sources
1. [Title](url) — краткое описание, дата

## Key facts
- Факт 1 [^1]

## Open questions

## References
[^1]: <quote>...</quote> — Source 1, дата
```

## Антипаттерны

- ❌ Спросить LLM по памяти вместо search
- ❌ Цитировать snippet как факт
- ❌ Single gigantic search query
- ❌ Не сохранять source URLs
