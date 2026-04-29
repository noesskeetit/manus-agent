---
name: browsing
description: Web browsing via Playwright (SPA, login, JS-heavy interactivity)
triggers: [войди в, login, залогинься, форма, register, spa, interactive, клик, scroll, интерактивный]
active_groups: [browser, research, file, memory, lifecycle, communication]
version: 1
---

# Skill: Web Browsing

Паттерн "работа со страницей которая требует JS / login / интерактив". Когда `page_fetch` (httpx) не справляется.

## Когда применять

- SPA (React/Vue/Angular)
- Бесконечный скролл, lazy-loading
- Логины, формы с CSRF/captcha
- Dynamic content
- Что-то нужно эмулировать кликами

## Когда НЕ применять

- Статический HTML — `page_fetch` (быстрее, дешевле)

## Workflow

1. `browser_navigate(url)`
2. `browser_extract()` — текст body
3. `browser_screenshot()` — иногда быстрее посмотреть скриншот
4. `browser_evaluate(js)` — самый точный способ извлечь данные:
   ```js
   Array.from(document.querySelectorAll('a')).slice(0, 50).map(a => ({
     text: a.innerText.trim().slice(0, 100),
     href: a.href
   }))
   ```
5. `browser_click(selector)` + `browser_fill(selector, text)` для интеракций
6. SPA с lazy-load — `browser_evaluate("window.scrollTo(0, document.body.scrollHeight)")` + `browser_extract()`

## Безопасность

- **Sensitive operations** (платёж, пароль, 2FA) → `message_ask_user(suggest_user_takeover)`
- **Credentials** — никогда не store в коде. Если нужен логин — env var, и удалить после.
- **Captcha** — попроси takeover.

## Антипаттерны

- ❌ browser для статических pages (медленно)
- ❌ Ввести пароль через browser_fill (логи)
- ❌ 50 кликов подряд без extract между
- ❌ Игнорировать `selector not found`
