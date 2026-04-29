"""Web search + URL fetch (без браузера). DuckDuckGo + httpx."""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult

logger = logging.getLogger("manus.tools.search")


# ---------- SSRF protection ----------

def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _check_url_safe(url: str) -> Optional[str]:
    """Проверяем URL на SSRF-риски. Возвращаем error message или None если OK."""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return f"unparseable URL: {e}"
    if parsed.scheme not in ("http", "https"):
        return f"only http/https allowed, got '{parsed.scheme}'"
    host = parsed.hostname
    if not host:
        return "no host in URL"
    # Прямой IP в URL
    try:
        ip = ipaddress.ip_address(host)
        if _is_private_ip(str(ip)):
            return f"refusing private/loopback/link-local IP: {ip}"
        return None
    except ValueError:
        pass  # Это hostname, резолвим
    # Резолвим все IP'и hostname и проверяем каждый
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip_str = info[4][0]
            if _is_private_ip(ip_str):
                return f"hostname '{host}' resolves to private IP {ip_str}"
    except socket.gaierror as e:
        return f"DNS resolution failed for '{host}': {e}"
    # IMDS endpoints (AWS/GCP)
    if host.lower() in ("metadata.google.internal", "169.254.169.254", "metadata"):
        return f"refusing IMDS metadata endpoint: {host}"
    return None


# ---------- info_search_web ----------

class InfoSearchArgs(BaseModel):
    query: str = Field(..., description="Запрос в стиле Google search (3-5 ключевых слов)")
    max_results: int = Field(8, description="Сколько результатов вернуть (1-20)")
    region: str = Field("ru-ru", description="Регион (ru-ru/wt-wt/us-en)")


class InfoSearchTool(Tool):
    group = "research"
    plan_safe = True   # сетевой call но read-only для нашей системы
    name = "info_search_web"
    description = ("Веб-поиск через DuckDuckGo. Возвращает список результатов: "
                   "title + url + snippet. ВАЖНО: snippet не источник истины, "
                   "ходи в page_fetch для деталей.")
    args_schema = InfoSearchArgs
    side_effects = True   # результаты search изменчивы — не кэшировать в idempotency cache

    def execute(self, args: InfoSearchArgs, ctx: ToolContext) -> ToolResult:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return ToolResult(content="ERROR: duckduckgo_search not installed", is_error=True)

        n = max(1, min(args.max_results, 20))
        try:
            with DDGS(timeout=15) as ddgs:
                results = list(ddgs.text(
                    args.query, region=args.region, safesearch="moderate", max_results=n,
                ))
        except Exception as e:
            return ToolResult(content=f"ERROR: search failed: {e}", is_error=True)

        if not results:
            return ToolResult(content=f"No results for: {args.query}")

        lines = [f"# Results for: {args.query}", ""]
        for i, r in enumerate(results, start=1):
            title = (r.get("title") or "").strip()
            url = (r.get("href") or r.get("url") or "").strip()
            snippet = (r.get("body") or "").strip().replace("\n", " ")
            if len(snippet) > 280:
                snippet = snippet[:280] + "..."
            lines.append(f"{i}. [{title}]({url})\n   {snippet}")
        return ToolResult(
            content="\n".join(lines),
            raw=results,
            metadata={"query": args.query, "count": len(results)},
        )


# ---------- page_fetch ----------

class PageFetchArgs(BaseModel):
    url: str = Field(..., description="Полный URL (с https://)")
    extract: str = Field(
        "text",
        description="text=plaintext (рекомендуется), html=сырой HTML, markdown=convert (попытка)",
    )
    max_chars: int = Field(20000, description="Максимум символов в content (остальное на диск)")


class PageFetchTool(Tool):
    group = "research"
    plan_safe = True
    name = "page_fetch"
    description = ("Скачать веб-страницу и извлечь текст. Большие страницы автоматически "
                   "сохраняются на диск, в context возвращается TL;DR + path.")
    args_schema = PageFetchArgs
    side_effects = True   # сетевой запрос → результат меняется со временем

    def execute(self, args: PageFetchArgs, ctx: ToolContext) -> ToolResult:
        url = args.url.strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(content=f"ERROR: invalid URL (need http/https): {url}", is_error=True)
        ssrf_err = _check_url_safe(url)
        if ssrf_err:
            return ToolResult(content=f"ERROR: refusing fetch — {ssrf_err}", is_error=True)

        # SSRF-safe redirect loop: проверяем каждый hop, max 5 редиректов.
        try:
            with httpx.Client(
                timeout=30.0,
                follow_redirects=False,   # ручной цикл — для per-hop SSRF check
                headers={
                    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru,en;q=0.7",
                },
            ) as client:
                current_url = url
                hops = 0
                while True:
                    r = client.get(current_url)
                    if r.is_redirect and hops < 5:
                        next_url = r.headers.get("location", "")
                        if not next_url:
                            break
                        # Относительные → абсолютные
                        if not next_url.startswith(("http://", "https://")):
                            from urllib.parse import urljoin
                            next_url = urljoin(current_url, next_url)
                        # Per-hop SSRF check
                        hop_err = _check_url_safe(next_url)
                        if hop_err:
                            return ToolResult(
                                content=f"ERROR: refusing redirect — {hop_err} (from {current_url} → {next_url})",
                                is_error=True,
                            )
                        current_url = next_url
                        hops += 1
                        continue
                    r.raise_for_status()
                    break
                html = r.text
                final_url = str(r.url)
        except httpx.HTTPStatusError as e:
            return ToolResult(content=f"ERROR: HTTP {e.response.status_code} for {url}",
                              is_error=True)
        except Exception as e:
            return ToolResult(content=f"ERROR: fetch failed for {url}: {e}", is_error=True)

        if args.extract == "html":
            extracted = html
        else:
            try:
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                    tag.decompose()
                if args.extract == "markdown":
                    # Попробуем сохранить заголовки и абзацы
                    parts: list[str] = []
                    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre"]):
                        txt = el.get_text(" ", strip=True)
                        if not txt:
                            continue
                        if el.name.startswith("h") and len(el.name) == 2:
                            level = "#" * int(el.name[1])
                            parts.append(f"{level} {txt}")
                        elif el.name == "li":
                            parts.append(f"- {txt}")
                        elif el.name == "pre":
                            parts.append(f"```\n{txt}\n```")
                        else:
                            parts.append(txt)
                    extracted = "\n\n".join(parts)
                else:
                    extracted = soup.get_text("\n", strip=True)
                    extracted = re.sub(r"\n{3,}", "\n\n", extracted)
            except Exception as e:
                logger.warning("HTML parse failed for %s: %s", url, e)
                extracted = html

        # Если большая страница — на диск целиком, в response — head
        full_size = len(extracted)
        path = None
        if full_size > args.max_chars:
            ws = ctx.workspace
            # Уникализуем имя через hash + agent iteration чтобы не было collision
            import hashlib as _h
            url_hash = _h.sha1(final_url.encode()).hexdigest()[:8]
            turn_id = getattr(ctx.agent_state, "iteration", None) if ctx.agent_state else None
            path = ws.dump_observation(
                name=f"page-{url_hash}-{re.sub(r'[^a-zA-Z0-9]+', '-', final_url)[:40]}",
                content=extracted,
                turn_id=turn_id,
            )
            head = extracted[: args.max_chars]
            content = (
                f"[Fetched {final_url} — {full_size} chars total, saved to {path}]\n\n"
                f"--- HEAD ({args.max_chars} chars) ---\n{head}\n... [truncated]"
            )
        else:
            content = f"[{final_url}]\n\n{extracted}"

        return ToolResult(
            content=content,
            raw=extracted,
            metadata={"url": final_url, "size": full_size,
                      "saved_to": str(path) if path else None},
            artifacts=[str(path)] if path else [],
        )


def make_search_tools() -> list[Tool]:
    return [InfoSearchTool(), PageFetchTool()]
