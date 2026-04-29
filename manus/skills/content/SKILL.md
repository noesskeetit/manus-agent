---
name: content
description: Long content generation (20+ items / lonread 5000+ words) without monotony
triggers: [напиши серию, посты, статья, лонгрид, контент, напиши книгу, генерация постов, tg-канал, telegram, блог, 30 постов, статьи]
active_groups: [file, memory, research, lifecycle, communication]
version: 1
---

# Skill: Long Content Generation

Паттерн "генерация большого объёма контента (20+ единиц или статья 5000+ слов) без монотонности".

## Принципы

1. **Структура → секции → драфты → финал.** Никогда не пиши единым потоком.
2. **Каждый кусок отдельным файлом.** `posts/01-intro.md`, и т.д.
3. **Variation policy** — не позволяй pattern lock-in.
4. **Reference-driven.**
5. **Никогда не сокращай при склейке.** Финальная длина ≥ сумма драфтов.

## Workflow для серии (например 30 постов)

1. **Стратегия**: pillars, ToV, ЦА, частота
2. **План постов** в `posts/_plan.md` с таблицей
3. **Идеи по постам** — один файл на пост `posts/NN-<slug>.md` (структура, hook, факты, variation notes)
4. **Drafting** — после каждого update `posts/_variation_log.md`
5. **Quality check** — прочитай 3 последних подряд, нет ли patterning?
6. **Compilation** — append'ом в final.md без сокращений

## Workflow для лонгрида

1. **Outline** в `outline.md`
2. **Section drafts** — каждая секция отдельным файлом
3. **Per-section research**
4. **Compilation** append'ом в `final.md`

## Variation log пример

```markdown
## Открытия
- "Знал ли ты что..." — пост 1, 7
- "5 фактов о..." — пост 3

## Структуры
- факт + объяснение + CTA — посты 1, 4

## Tone
- спокойный — посты 1-3
- юмор — пост 7
```

## Антипаттерны

- ❌ Писать сразу финальный документ
- ❌ Не вести variation log
- ❌ Сокращать при склейке
- ❌ Утверждения без reference
- ❌ Одна структура для всех 30 постов
