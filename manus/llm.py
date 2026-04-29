"""LLM client: OpenAI-compatible chat с robust retry, tool calling, normalization для Cloud.ru FM."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError, BadRequestError, InternalServerError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from .config import CONFIG, ModelSpec, get_model

logger = logging.getLogger("manus.llm")


# ---------- Типы ----------

@dataclass
class ToolCall:
    """Нормализованный tool call от LLM."""
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""           # сырая строка от модели (для повтора при ошибке)
    truncated: bool = False           # arguments не валидный JSON → возможно стрим обрезался


@dataclass
class LLMResponse:
    """Унифицированный ответ LLM."""
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"        # stop|tool_calls|length|content_filter
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    raw_message: dict[str, Any] | None = None  # для записи в session


# ---------- Клиент ----------

class LLMClient:
    """OpenAI-совместимый клиент с защитой от Cloud.ru квирков (reasoning_content, обрезанные tool_calls)."""

    def __init__(self, model: ModelSpec | str = CONFIG.executor_model):
        if isinstance(model, str):
            model = get_model(model)
        self.model = model
        api_key = os.environ.get(model.api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"API key not found in env var ${model.api_key_env}. "
                "Set it in your shell, in .env in the project root, or in ~/.config/manus/secrets.env"
            )
        self._client = OpenAI(
            api_key=api_key,
            base_url=model.api_base,
            timeout=CONFIG.llm_request_timeout_sec,
            max_retries=0,  # своя retry-стратегия через tenacity
        )

    @retry(
        retry=retry_if_exception_type((APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)),
        stop=stop_after_attempt(CONFIG.llm_retry_max_attempts),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str | dict] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Один LLM-запрос с tool calling. Возвращает нормализованный LLMResponse.

        - Стрипает reasoning_content (Cloud.ru тащит в response даже при thinkingDefault: off)
        - Парсит tool_calls в нашу структуру
        - Помечает truncated tool_calls (broken JSON в arguments)
        """
        params: dict[str, Any] = {
            "model": self.model.id,
            "messages": messages,
            "temperature": temperature if temperature is not None else CONFIG.llm_temperature,
            "max_tokens": max_tokens or CONFIG.llm_max_tokens_per_turn,
        }
        if tools:
            params["tools"] = tools
            if tool_choice is not None:
                params["tool_choice"] = tool_choice

        # Cloud.ru-specific: thinking off через extra_body
        # FM API: thinking{"type":"disabled"}; vLLM: chat_template_kwargs.enable_thinking=false
        if "modelrun.inference.cloud.ru" in self.model.api_base:
            params["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False},
            }
        elif "cloud.ru" in self.model.api_base:
            params["extra_body"] = {
                "thinking": {"type": "disabled"},
            }

        t0 = time.monotonic()
        try:
            completion: ChatCompletion = self._client.chat.completions.create(**params)
        except BadRequestError as e:
            # Some Cloud.ru models reject extra_body keys — retry без него
            logger.warning("BadRequest with extra_body, retrying without it: %s", e)
            params.pop("extra_body", None)
            completion = self._client.chat.completions.create(**params)

        latency = time.monotonic() - t0
        msg: ChatCompletionMessage = completion.choices[0].message
        finish_reason = completion.choices[0].finish_reason or "stop"

        # Парсим tool calls
        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                raw_args = tc.function.arguments or ""
                truncated = False
                args: dict[str, Any] = {}
                stripped = raw_args.strip()
                if not stripped:
                    # Пустые args — НЕ truncated, валидный кейс (idle, ls без args)
                    args = {}
                else:
                    try:
                        args = json.loads(stripped)
                        if not isinstance(args, dict):
                            # Модель вернула не-dict (массив/строка) — truncated/malformed
                            truncated = True
                            logger.warning("tool_call arguments not a dict (id=%s, name=%s): %r",
                                           tc.id, tc.function.name, stripped[:200])
                            args = {}
                    except json.JSONDecodeError:
                        truncated = True
                        logger.warning("Truncated tool_call arguments (id=%s, name=%s): %r",
                                       tc.id, tc.function.name, stripped[:200])
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                    raw_arguments=raw_args,
                    truncated=truncated,
                ))

        # Стрипаем reasoning_content (если есть в raw)
        raw_msg = msg.model_dump()
        raw_msg.pop("reasoning_content", None)
        # Также удаляем function_call (deprecated)
        raw_msg.pop("function_call", None)

        usage = completion.usage
        resp = LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            model=completion.model,
            raw_message=raw_msg,
        )
        logger.info(
            "LLM call %s: %dms tokens=%d/%d finish=%s tool_calls=%d",
            self.model.short, int(latency * 1000),
            resp.prompt_tokens, resp.completion_tokens,
            resp.finish_reason, len(resp.tool_calls),
        )
        return resp


# ---------- Helpers для построения messages ----------

def system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def assistant_message_from_response(resp: LLMResponse) -> dict[str, Any]:
    """Конвертирует LLMResponse в OpenAI-style message для следующего turn.

    ВАЖНО: для truncated tool_calls подставляем `arguments="{}"` чтобы Cloud.ru/vLLM
    не зашумел на broken JSON в истории. Сообщение об ошибке потом летит через tool_result.
    """
    msg: dict[str, Any] = {"role": "assistant", "content": resp.content or None}
    if resp.tool_calls:
        tool_calls_out: list[dict] = []
        for tc in resp.tool_calls:
            if tc.truncated:
                # Подставляем валидный пустой JSON, реальное сообщение об ошибке идёт в tool_result
                args_str = "{}"
            else:
                args_str = tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False)
            tool_calls_out.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": args_str},
            })
        msg["tool_calls"] = tool_calls_out
    return msg


def tool_result_message(tool_call_id: str, content: str | dict) -> dict[str, Any]:
    """Стандартный tool-result message."""
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
