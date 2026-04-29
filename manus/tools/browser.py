"""Browser tools на Playwright. Lazy: подключаются только если playwright установлен."""
from __future__ import annotations

import atexit
import logging
from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult

logger = logging.getLogger("manus.tools.browser")


# Глобальный browser context — один на процесс агента
_PW_STATE: dict = {"playwright": None, "browser": None, "context": None, "page": None}
_ATEXIT_REGISTERED = False


def _ensure_browser():
    """Lazy init Playwright + Chromium. Возвращает (page, context)."""
    global _ATEXIT_REGISTERED
    if _PW_STATE["page"] is not None:
        return _PW_STATE["page"], _PW_STATE["context"]
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1366, "height": 800},
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        locale="ru-RU",
    )
    page = context.new_page()
    _PW_STATE.update({"playwright": pw, "browser": browser, "context": context, "page": page})
    if not _ATEXIT_REGISTERED:
        atexit.register(_shutdown_browser)
        _ATEXIT_REGISTERED = True
    return page, context


def _shutdown_browser():
    page = _PW_STATE.pop("page", None)
    ctx = _PW_STATE.pop("context", None)
    br = _PW_STATE.pop("browser", None)
    pw = _PW_STATE.pop("playwright", None)
    for o in (page, ctx, br):
        try:
            if o is not None:
                o.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


# ---------- browser_navigate ----------

class BrowserNavigateArgs(BaseModel):
    url: str = Field(..., description="Полный URL (с https://)")
    wait: str = Field("load", description="load|domcontentloaded|networkidle")


class BrowserNavigateTool(Tool):
    group = "browser"
    name = "browser_navigate"
    description = "Открыть URL в headless Chromium. Возвращает title + первый текст страницы."
    args_schema = BrowserNavigateArgs
    side_effects = True   # навигация меняет browser state — не кэшировать idempotent

    def execute(self, args: BrowserNavigateArgs, ctx: ToolContext) -> ToolResult:
        # SSRF защита для browser navigation
        from .search import _check_url_safe
        ssrf_err = _check_url_safe(args.url)
        if ssrf_err:
            return ToolResult(content=f"ERROR: refusing navigation — {ssrf_err}", is_error=True)
        try:
            page, _ = _ensure_browser()
            page.goto(args.url, wait_until=args.wait, timeout=45_000)
            title = page.title()
            text_preview = page.locator("body").inner_text(timeout=5000)[:3000]
            return ToolResult(
                content=f"[{args.url}] title: {title}\n\n--- body preview ---\n{text_preview}",
                metadata={"url": args.url, "title": title},
            )
        except Exception as e:
            return ToolResult(content=f"ERROR: navigate failed: {e}", is_error=True)


# ---------- browser_click ----------

class BrowserClickArgs(BaseModel):
    selector: str = Field(..., description="CSS / XPath селектор")
    wait: int = Field(5000, description="Таймаут ms")


class BrowserClickTool(Tool):
    group = "browser"
    name = "browser_click"
    description = "Кликнуть элемент по селектору"
    args_schema = BrowserClickArgs
    side_effects = True

    def execute(self, args: BrowserClickArgs, ctx: ToolContext) -> ToolResult:
        try:
            page, _ = _ensure_browser()
            page.click(args.selector, timeout=args.wait)
            return ToolResult(content=f"OK: clicked {args.selector}")
        except Exception as e:
            return ToolResult(content=f"ERROR: click failed: {e}", is_error=True)


# ---------- browser_fill ----------

class BrowserFillArgs(BaseModel):
    selector: str
    text: str
    press_enter: bool = Field(False)


class BrowserFillTool(Tool):
    group = "browser"
    name = "browser_fill"
    description = "Заполнить input/textarea (clear + type)"
    args_schema = BrowserFillArgs
    side_effects = True

    def execute(self, args: BrowserFillArgs, ctx: ToolContext) -> ToolResult:
        try:
            page, _ = _ensure_browser()
            page.fill(args.selector, args.text)
            if args.press_enter:
                page.press(args.selector, "Enter")
            return ToolResult(content=f"OK: filled {args.selector} ({len(args.text)} chars)")
        except Exception as e:
            return ToolResult(content=f"ERROR: fill failed: {e}", is_error=True)


# ---------- browser_extract ----------

class BrowserExtractArgs(BaseModel):
    selector: Optional[str] = Field(None, description="Если задан — текст из этого селектора, иначе всё body")
    max_chars: int = Field(20000)


class BrowserExtractTool(Tool):
    group = "browser"
    name = "browser_extract"
    description = "Получить текст с текущей страницы (всю или по селектору)."
    args_schema = BrowserExtractArgs
    side_effects = True   # зависит от mutable state страницы — не idempotent

    def execute(self, args: BrowserExtractArgs, ctx: ToolContext) -> ToolResult:
        try:
            page, _ = _ensure_browser()
            if args.selector:
                txt = page.locator(args.selector).inner_text(timeout=5000)
            else:
                txt = page.locator("body").inner_text(timeout=5000)
            url = page.url
            full_size = len(txt)
            saved = None
            if full_size > args.max_chars:
                ws = ctx.workspace
                import hashlib as _h, re as _re
                url_hash = _h.sha1(url.encode()).hexdigest()[:8]
                turn_id = getattr(ctx.agent_state, "iteration", None) if ctx.agent_state else None
                saved = ws.dump_observation(
                    name=f"browser-{url_hash}-{_re.sub(r'[^a-zA-Z0-9]+', '-', url)[:40]}",
                    content=txt, turn_id=turn_id,
                )
                head = txt[:args.max_chars]
                content = (f"[{url} — {full_size} chars total, saved to {saved}]\n\n"
                           f"--- HEAD ---\n{head}\n... [truncated]")
            else:
                content = f"[{url}]\n{txt}"
            return ToolResult(content=content, raw=txt,
                              metadata={"url": url, "size": full_size,
                                        "saved_to": str(saved) if saved else None})
        except Exception as e:
            return ToolResult(content=f"ERROR: extract failed: {e}", is_error=True)


# ---------- browser_screenshot ----------

class BrowserScreenshotArgs(BaseModel):
    path: Optional[str] = Field(None, description="Куда сохранить (default: workspace/screenshots/...)")
    full_page: bool = Field(False)


class BrowserScreenshotTool(Tool):
    group = "browser"
    name = "browser_screenshot"
    description = "Сделать скриншот текущей страницы"
    args_schema = BrowserScreenshotArgs
    side_effects = True   # пишет файл

    def execute(self, args: BrowserScreenshotArgs, ctx: ToolContext) -> ToolResult:
        try:
            page, _ = _ensure_browser()
            from datetime import datetime
            from pathlib import Path
            if args.path:
                target = Path(args.path)
                if not target.is_absolute():
                    target = ctx.workspace.root / target
            else:
                target = ctx.workspace.root / "screenshots" / f"shot-{datetime.now().strftime('%H%M%S')}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target), full_page=args.full_page)
            return ToolResult(content=f"OK: screenshot saved to {target}", artifacts=[str(target)])
        except Exception as e:
            return ToolResult(content=f"ERROR: screenshot failed: {e}", is_error=True)


# ---------- browser_eval ----------

class BrowserEvalArgs(BaseModel):
    script: str = Field(..., description="JavaScript expression. Возвращает результат как JSON.")


class BrowserEvalTool(Tool):
    group = "browser"
    name = "browser_evaluate"
    description = (
        "Выполнить JS в browser context и вернуть результат. "
        "DANGEROUS: JS может извлечь cookies/localStorage всех сайтов в этом browser context. "
        "Запрещён по умолчанию (требует MANUS_ALLOW_BROWSER_EVAL=true)."
    )
    args_schema = BrowserEvalArgs
    side_effects = True

    def execute(self, args: BrowserEvalArgs, ctx: ToolContext) -> ToolResult:
        import os
        if os.environ.get("MANUS_ALLOW_BROWSER_EVAL", "").lower() not in ("1", "true", "yes"):
            return ToolResult(
                content=("ERROR: browser_evaluate is disabled by default for security. "
                         "Set env MANUS_ALLOW_BROWSER_EVAL=true to enable."),
                is_error=True,
            )
        try:
            page, _ = _ensure_browser()
            res = page.evaluate(args.script)
            import json as _j
            return ToolResult(content=f"{_j.dumps(res, ensure_ascii=False, default=str)[:5000]}")
        except Exception as e:
            return ToolResult(content=f"ERROR: evaluate failed: {e}", is_error=True)


def make_browser_tools() -> list[Tool]:
    try:
        import playwright  # noqa
    except ImportError:
        logger.info("playwright not installed — skipping browser tools")
        return []
    return [
        BrowserNavigateTool(),
        BrowserClickTool(),
        BrowserFillTool(),
        BrowserExtractTool(),
        BrowserScreenshotTool(),
        BrowserEvalTool(),
    ]
