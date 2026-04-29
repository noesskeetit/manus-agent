"""Сообщения пользователю — Telegram (notify/ask) + локальный stdout fallback.

Если CONFIG.tg_enabled == False, ask блокирует на input(), notify — печатает в stdout.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from ..config import CONFIG
from .base import Tool, ToolContext, ToolResult

logger = logging.getLogger("manus.tools.message")


# ---------- TG helpers (без MCP, прямо через Bot API) ----------

def _tg_post(method: str, data: dict, timeout: float = 30.0) -> dict:
    """Telegram Bot API call. Token в URL по требованию TG API, но мы маскируем при ошибках."""
    if not CONFIG.tg_enabled:
        raise RuntimeError("Telegram is not configured (set MANUS_TG_BOT_TOKEN, MANUS_TG_USER_ID)")
    # TG API требует token в URL — Authorization header им не работает.
    # Минимизируем риск утечки: явно ловим exceptions, не давая httpx залогировать URL.
    url = f"https://api.telegram.org/bot{CONFIG.tg_bot_token}/{method}"
    try:
        r = httpx.post(url, json=data, timeout=timeout)
    except httpx.HTTPError as e:
        # НЕ логируем e.request.url — там token. Маскируем имя метода только.
        raise RuntimeError(f"Telegram network error on method='{method}': {type(e).__name__}") from None
    try:
        js = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram non-JSON response on method='{method}', status={r.status_code}")
    if not js.get("ok"):
        raise RuntimeError(f"Telegram error: {js.get('error_code')} {js.get('description')}")
    return js["result"]


def _tg_send_text(chat_id: str | int, text: str,
                  reply_markup: Optional[dict] = None,
                  parse_mode: str = "HTML") -> dict:
    payload: dict = {"chat_id": chat_id, "text": text[:4096]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _tg_post("sendMessage", payload)


# ---------- Tools ----------

class NotifyArgs(BaseModel):
    text: str = Field(..., description="Текст уведомления (поддерживает HTML: <b>, <i>, <code>, <a>). До 4096 символов.")
    attachments: Optional[list[str]] = Field(None, description="Список путей к файлам (для записи в журнал агента, не отправляются в TG)")


class NotifyTool(Tool):
    group = "communication"
    always_available = True
    name = "message_notify_user"
    description = (
        "Non-blocking уведомление пользователю. Используй для рапортов: старт задачи, завершение фазы, "
        "важная находка, финал. НЕ ЧАСТИТЬ — ~5-10 сообщений на всю задачу."
    )
    args_schema = NotifyArgs
    side_effects = True

    def execute(self, args: NotifyArgs, ctx: ToolContext) -> ToolResult:
        text = args.text
        if CONFIG.tg_enabled:
            try:
                _tg_send_text(CONFIG.tg_user_id, text)
                return ToolResult(content=f"OK: notified user via Telegram ({len(text)} chars)")
            except Exception as e:
                logger.warning("TG notify failed, fallback to stdout: %s", e)

        # Fallback: stdout
        sys.stdout.write(f"\n[manus → user] {text}\n")
        sys.stdout.flush()
        return ToolResult(content=f"OK: printed to stdout (TG disabled, len={len(text)})")


class AskArgs(BaseModel):
    text: str = Field(..., description="Вопрос пользователю")
    options: Optional[list[str]] = Field(None, description="Опции выбора (опционально, до 8)")
    timeout_sec: int = Field(3600, description="Сколько максимум ждать ответа (сек)")


class AskTool(Tool):
    group = "communication"
    name = "message_ask_user"
    description = (
        "BLOCKING: задать вопрос пользователю и ждать ответа. Использовать ТОЛЬКО когда:\n"
        "(а) есть неоднозначность которую сам не разрулишь\n"
        "(б) действие с побочкой (публикация/платёж/удаление)\n"
        "(в) выбор без объективного критерия\n"
        "Не задавай по мелочам."
    )
    args_schema = AskArgs
    side_effects = True

    def execute(self, args: AskArgs, ctx: ToolContext) -> ToolResult:
        if CONFIG.tg_enabled:
            return self._ask_via_tg(args)
        return self._ask_via_stdin(args)

    @staticmethod
    def _ask_via_stdin(args: AskArgs) -> ToolResult:
        sys.stdout.write(f"\n[manus → user] {args.text}\n")
        if args.options:
            for i, o in enumerate(args.options, 1):
                sys.stdout.write(f"  {i}. {o}\n")
        sys.stdout.write("Ответ: ")
        sys.stdout.flush()
        try:
            line = input()
        except EOFError:
            return ToolResult(content=f"TIMEOUT: no input (EOF)", is_error=False)
        return ToolResult(
            content=f"answer: {line.strip()}",
            metadata={"answer": line.strip(), "via": "stdin"},
        )

    @staticmethod
    def _ask_via_tg(args: AskArgs) -> ToolResult:
        # Шлём вопрос
        reply_markup = None
        if args.options:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": o[:64], "callback_data": f"ans:{i}"}]
                    for i, o in enumerate(args.options[:8])
                ]
            }
        try:
            _tg_send_text(CONFIG.tg_user_id, args.text + "\n\n<i>(жду ответ)</i>",
                          reply_markup=reply_markup)
        except Exception as e:
            return ToolResult(content=f"ERROR: ask failed: {e}", is_error=True)

        # Long-poll updates пока не придёт ответ
        deadline = time.monotonic() + args.timeout_sec
        offset = 0
        while time.monotonic() < deadline:
            try:
                wait = min(20, int(deadline - time.monotonic()))
                if wait <= 0:
                    break
                updates = _tg_post(
                    "getUpdates",
                    {"offset": offset + 1, "timeout": wait,
                     "allowed_updates": ["message", "callback_query"]},
                    timeout=wait + 30,
                )
            except Exception as e:
                logger.warning("getUpdates error: %s", e)
                time.sleep(2)
                continue

            if not isinstance(updates, list):
                continue
            for upd in updates:
                offset = max(offset, upd.get("update_id", 0))
                # Сначала callback_query (button)
                cq = upd.get("callback_query")
                if cq and str(cq.get("from", {}).get("id")) == str(CONFIG.tg_user_id):
                    data = cq.get("data", "")
                    if data.startswith("ans:") and args.options:
                        idx = int(data[4:])
                        if 0 <= idx < len(args.options):
                            try:
                                _tg_post("answerCallbackQuery",
                                         {"callback_query_id": cq["id"], "text": "Принято"})
                            except Exception:
                                pass
                            return ToolResult(
                                content=f"answer: {args.options[idx]}",
                                metadata={"answer": args.options[idx], "via": "tg-button"},
                            )
                # Текстовое сообщение
                msg = upd.get("message")
                if msg and str(msg.get("chat", {}).get("id")) == str(CONFIG.tg_user_id):
                    text = (msg.get("text") or "").strip()
                    if text:
                        return ToolResult(
                            content=f"answer: {text}",
                            metadata={"answer": text, "via": "tg-text"},
                        )
        return ToolResult(content="TIMEOUT: no answer within timeout", metadata={"timeout": True})


def make_message_tools() -> list[Tool]:
    return [NotifyTool(), AskTool()]
