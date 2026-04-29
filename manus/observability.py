"""Observability — Phoenix + OpenInference + custom spans для tool calls.

Lazy: всё подключается только если установлены `[observability]` extras.

Использование:
    from manus.observability import setup_phoenix, trace_tool_call

    setup_phoenix(launch_local=True)  # запустит локальный Phoenix UI на :6006
    # или: setup_phoenix(launch_local=False)  # подключение к существующему collector

В Agent loop tool calls автоматически оборачиваются в spans если phoenix доступен.
LLM calls трейсятся автоматически через OpenInference instrumentor.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

logger = logging.getLogger("manus.observability")

_TRACER = None
_PHOENIX_SESSION = None
_PHOENIX_URL: Optional[str] = None


def setup_phoenix(launch_local: bool = True,
                  project_name: str = "manus-agent",
                  endpoint: Optional[str] = None) -> Optional[str]:
    """Init Phoenix + OpenInference. Возвращает Phoenix UI URL или None если не удалось.

    Args:
        launch_local: True — попытаться запустить embedded Phoenix server (если установлен
            full `arize-phoenix` пакет). Если только `arize-phoenix-otel` — отправляем
            traces в существующий collector (либо через docker, либо Phoenix Cloud).
        project_name: имя проекта в Phoenix UI
        endpoint: URL OTLP collector (только при launch_local=False)

    Установка:
        # Lite (только tracer + instrumentation, нужен внешний collector):
        pip install arize-phoenix-otel openinference-instrumentation-openai
        # + Phoenix server отдельно:
        docker run -d -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest

        # Full (embedded server + UI в одном venv, требует pandas):
        pip install arize-phoenix
    """
    global _TRACER, _PHOENIX_SESSION, _PHOENIX_URL

    if _TRACER is not None:
        return _PHOENIX_URL

    # Проверяем что хотя бы otel-only вариант установлен
    try:
        from phoenix.otel import register
    except ImportError:
        logger.warning(
            "Phoenix tracer not installed — skipping observability setup. "
            "Install: pip install arize-phoenix-otel openinference-instrumentation-openai"
        )
        return None

    # 1. Embedded server: только если установлен full pkg
    if launch_local:
        embedded_ok = False
        try:
            import phoenix as px  # type: ignore
            if hasattr(px, "launch_app"):
                _PHOENIX_SESSION = px.launch_app()
                _PHOENIX_URL = (_PHOENIX_SESSION.url if hasattr(_PHOENIX_SESSION, "url")
                                else "http://localhost:6006")
                logger.info("Phoenix UI launched embedded at %s", _PHOENIX_URL)
                embedded_ok = True
        except ImportError:
            pass
        except Exception as e:
            logger.warning("Failed to launch embedded Phoenix: %s", e)

        if not embedded_ok:
            logger.info("Embedded Phoenix unavailable (only `arize-phoenix-otel` installed). "
                        "External collector: docker run -d -p 6006:6006 -p 4317:4317 arizephoenix/phoenix")
            _PHOENIX_URL = endpoint or os.environ.get(
                "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"
            )
    else:
        _PHOENIX_URL = endpoint or os.environ.get(
            "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"
        )

    # 2. Register tracer + auto-instrument OpenAI
    try:
        tracer_provider = register(
            project_name=project_name,
            endpoint=f"{_PHOENIX_URL.rstrip('/')}/v1/traces",
            set_global_tracer_provider=True,
            verbose=False,
        )
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
            logger.info("OpenAI auto-instrumentation enabled")
        except Exception as e:
            logger.warning("OpenAI auto-instrumentation failed: %s", e)

        _TRACER = tracer_provider.get_tracer("manus-agent")
    except Exception as e:
        logger.exception("Phoenix tracer setup failed: %s", e)
        return _PHOENIX_URL

    return _PHOENIX_URL


@contextmanager
def trace_tool_call(tool_name: str, args: dict, task_id: str = "",
                    iteration: int = 0):
    """Context manager — оборачивает tool execution в OpenTelemetry span.

    Если tracer не инициализирован — no-op (yield пустой context).
    """
    if _TRACER is None:
        yield None
        return
    try:
        # OpenInference TOOL span (для удобной фильтрации в Phoenix)
        with _TRACER.start_as_current_span(
            f"tool.{tool_name}",
            attributes={
                "openinference.span.kind": "TOOL",
                "tool.name": tool_name,
                "tool.parameters": _safe_json(args),
                "manus.task_id": task_id,
                "manus.iteration": iteration,
            },
        ) as span:
            yield span
    except Exception as e:
        logger.warning("trace_tool_call wrapping failed: %s", e)
        yield None


@contextmanager
def trace_iteration(task_id: str, iteration: int):
    """Span на одну итерацию agent loop'а."""
    if _TRACER is None:
        yield None
        return
    try:
        with _TRACER.start_as_current_span(
            f"iter.{iteration:04d}",
            attributes={
                "openinference.span.kind": "AGENT",
                "manus.task_id": task_id,
                "manus.iteration": iteration,
            },
        ) as span:
            yield span
    except Exception as e:
        logger.warning("trace_iteration failed: %s", e)
        yield None


def annotate_span_output(span: Any, output: str, is_error: bool = False) -> None:
    """Дописать output / error к span после execute()."""
    if span is None:
        return
    try:
        if is_error:
            span.set_attribute("tool.is_error", True)
            span.set_attribute("output.value", (output or "")[:8000])
        else:
            span.set_attribute("output.value", (output or "")[:8000])
    except Exception:
        pass


def _safe_json(obj: Any, max_len: int = 4000) -> str:
    import json
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s[:max_len]


def is_enabled() -> bool:
    return _TRACER is not None


def phoenix_url() -> Optional[str]:
    return _PHOENIX_URL
