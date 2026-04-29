# Playbook: Web browsing через Playwright

Паттерн "работа со страницей которая требует JS / login / интерактив". Когда `page_fetch` (httpx) не справляется.

## Когда применять

- SPA (React/Vue/Angular) — серверный HTML пустой
- Бесконечный скролл, lazy-loading
- Логины, формы с CSRF/captcha
- Dynamic content (раскрывающиеся блоки, dropdowns, search-as-you-type)
- Что-то нужно эмулировать кликами

## Когда НЕ применять

- Простой статический HTML — используй `page_fetch` (быстрее, дешевле)
- API-документация, плоские текстовые сайты — `page_fetch`

## Workflow

1. **`browser_navigate(url)`** — загрузить страницу
2. **`browser_extract()`** — получить текст body. Если пусто/мало (SPA не отрендерился) — подожди:
3. **`browser_screenshot()`** — иногда быстрее посмотреть скриншот чем читать DOM
4. **`browser_evaluate(js)`** — самый точный способ извлечь данные:
   ```js
   // Пример: получить все ссылки
   Array.from(document.querySelectorAll('a')).slice(0, 50).map(a => ({
     text: a.innerText.trim().slice(0, 100),
     href: a.href
   }))
   ```
5. **`browser_click(selector)`** + **`browser_fill(selector, text)`** для интеракций
6. **При SPA с lazy-load** — несколько `browser_evaluate("window.scrollTo(0, document.body.scrollHeight)")` подряд + `browser_extract()`

## Lifecycle

Browser context — singleton на процесс агента. Все вызовы в рамках одной сессии шарят cookies, localStorage, открытые табы.

При резюме после crash браузер придётся открывать заново — текущие сессии не персистентны (это deliberately, чтобы не было stale state).

## Безопасность

- **Sensitive operations** (платёж, пароль, 2FA) → `message_ask_user` с `suggest_user_takeover`. Пользователь сам выполнит шаг.
- **Credentials** — никогда не store в коде/файлах workspace. Если нужен логин — env var, и тут же удалить из контекста после использования.
- **Captcha** — не пытайся обходить, попроси пользователя.

## Полезные паттерны

```python
# 1. Extract structured data со страницы
browser_evaluate("""
  Array.from(document.querySelectorAll('.product-card')).map(c => ({
    name: c.querySelector('.name')?.innerText,
    price: c.querySelector('.price')?.innerText,
    url: c.querySelector('a')?.href
  }))
""")

# 2. Wait for element (если page рендерится медленно)
browser_evaluate("""
  new Promise(r => {
    const check = () => document.querySelector('.results') ? r('ready') : setTimeout(check, 200);
    check();
  })
""")

# 3. Pagination
for i in range(5):
    browser_click(".pagination .next")
    browser_extract()  # будет накапливать в observations/
```

## Антипаттерны

- ❌ Использовать browser для статических pages (медленно, дорого)
- ❌ Ввести пароль в `browser_fill` (сохранится в логах) — попроси takeover
- ❌ Делать 50 кликов подряд без `browser_extract` между — не видишь что происходит
- ❌ Игнорировать ошибки `selector not found` — иногда DOM меняется, нужен `wait`
- ❌ Закрывать browser_close посреди задачи (потеряешь cookies/state)
