# Cloud.ru FM API — quick reference

## Endpoints

- **FM API**: `https://foundation-models.api.cloud.ru/v1` (OpenAI-совместимый)
- **ML Inference vLLM**: индивидуальные UUID-host'ы на `*.modelrun.inference.cloud.ru`
- **cloudru-proxy** (если запущен): `http://127.0.0.1:19000/v1` — стрипает reasoning_content + триммит tool_results

## Модели и их особенности

| Model | Context | Tool calling | Лучшие сценарии |
|---|---|---|---|
| Qwen/Qwen3-Coder-Next | 256k | ✓ native | Default executor, coding-heavy |
| MiniMaxAI/MiniMax-M2 | 192k | ✓ native | Planner для длинных задач |
| zai-org/GLM-4.7 | 200k | ✗ XML thinking | ТОЛЬКО для compaction/summary (без tool) |
| qwen36-27b-fp8 (vLLM) | 128k | ✓ native | Fallback если FM API ключ disabled |

## Гочтa

- **`thinkingDefault: off`** обязателен для FM API, иначе `content: null`
- **`reasoning_content`** возвращается даже при thinking off (особенно MiniMax-M2) → cloudru-proxy strip'ит
- **`chat_template_kwargs.enable_thinking=False`** для vLLM endpoints (отличается от FM)
- **`no_proxy`** должен включать FM API hostname, иначе корпоративный прокси блокирует
- vLLM **требует system message в начале** (не несколько посередине)

## Auth

- API key через env `LLM_API_KEY` (формат: `<base64>.<hex>`)
- Тот же ключ работает для FM API и ML Inference vLLM
