"""Deploy tools — публичный URL для локального сервиса.

`deploy_expose_port`: эфемерный туннель через `cloudflared tunnel --url`. Без аккаунта.
`deploy_apply_deployment`: пакетирует static-сайт в zip → возвращает путь
   (для real-deployment пользователю; полноценный provider не реализуем в MVP).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


def _has_cloudflared() -> bool:
    return subprocess.run(["which", "cloudflared"], capture_output=True).returncode == 0


# ---------- deploy_expose_port ----------

class DeployExposeArgs(BaseModel):
    port: int = Field(..., description="Локальный порт сервиса")
    wait_url_sec: int = Field(20, description="Сколько секунд ждать публичный URL от cloudflared")


class DeployExposeTool(Tool):
    group = "deploy"
    name = "deploy_expose_port"
    description = (
        "Открыть локальный порт публичным URL через `cloudflared tunnel --url`. "
        "Запускает tunnel в фоне (отдельный tmux-сессия `manus-cloudflared-<port>`), "
        "возвращает trycloudflare.com URL. ВАЖНО: убедись что сервис уже запущен "
        "и слушает на 0.0.0.0:<port> (не 127.0.0.1). URL временный (трюки до пары часов)."
    )
    args_schema = DeployExposeArgs
    side_effects = True

    def execute(self, args: DeployExposeArgs, ctx: ToolContext) -> ToolResult:
        # Approval gate: critical security boundary (Manus had RCE via expose_port).
        # По умолчанию требует MANUS_ALLOW_DEPLOY_EXPOSE=true чтобы агент сам мог exposить.
        # Для interactive — message_ask_user "Разрешить публичный URL для порта X?".
        approval_env = os.environ.get("MANUS_ALLOW_DEPLOY_EXPOSE", "").lower()
        if approval_env not in ("1", "true", "yes"):
            return ToolResult(
                content=(
                    f"ERROR: deploy_expose_port disabled by default for security "
                    f"(it was an RCE vector in real Manus). "
                    f"Either: (1) call message_ask_user with explicit approval request, "
                    f"(2) set env MANUS_ALLOW_DEPLOY_EXPOSE=true, "
                    f"(3) skip and use static deploy_apply_deployment instead."
                ),
                is_error=True,
            )
        if not _has_cloudflared():
            return ToolResult(
                content="ERROR: cloudflared not installed. brew install cloudflared",
                is_error=True,
            )
        if args.port <= 0 or args.port > 65535:
            return ToolResult(content=f"ERROR: invalid port {args.port}", is_error=True)

        log_path = ctx.workspace.root / f"cloudflared-{args.port}.log"
        cmd = (f"cloudflared tunnel --no-autoupdate --url http://localhost:{args.port} "
               f"> {shlex.quote(str(log_path))} 2>&1 &")
        # Запускаем через subprocess.Popen — нужен фон
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Ждём появления URL в логе
        deadline = time.monotonic() + args.wait_url_sec
        url: Optional[str] = None
        while time.monotonic() < deadline:
            if log_path.exists():
                txt = log_path.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
                if m:
                    url = m.group(0)
                    break
            time.sleep(0.5)

        if not url:
            tail = ""
            if log_path.exists():
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            return ToolResult(
                content=(f"ERROR: cloudflared не вернул URL за {args.wait_url_sec}s.\n"
                         f"Log tail:\n{tail}"),
                is_error=True,
                metadata={"log": str(log_path), "port": args.port},
            )

        return ToolResult(
            content=(f"OK: port {args.port} exposed at {url}\n"
                     f"NOTE: this is an ephemeral trycloudflare URL (no auth). "
                     f"Tunnel log: {log_path}"),
            artifacts=[str(log_path)],
            metadata={"url": url, "port": args.port, "log": str(log_path),
                      "pid": proc.pid},
        )


# ---------- deploy_apply_deployment ----------

class DeployApplyArgs(BaseModel):
    type: str = Field(..., description="static = static website, nextjs = Next.js app",
                      pattern="^(static|nextjs)$")
    local_dir: str = Field(..., description="Путь к директории (для static — содержит index.html)")
    package_only: bool = Field(True, description="True = создать zip и вернуть путь "
                                                  "(real deployment провайдер пока не подключён, "
                                                  "пользователь сам загружает в Cloud.ru/Vercel/...)")


class DeployApplyTool(Tool):
    group = "deploy"
    name = "deploy_apply_deployment"
    description = (
        "Запаковать static website или Next.js приложение в zip-файл для последующего "
        "deployment пользователем. По умолчанию package_only=True (только zip, deployment "
        "провайдер не подключён). Возвращает путь к zip — отдай пользователю через "
        "message_notify_user с attachments."
    )
    args_schema = DeployApplyArgs
    side_effects = True

    def execute(self, args: DeployApplyArgs, ctx: ToolContext) -> ToolResult:
        d = Path(args.local_dir).expanduser()
        if not d.is_absolute():
            d = ctx.workspace.root / d
        if not d.exists() or not d.is_dir():
            return ToolResult(content=f"ERROR: directory not found: {d}", is_error=True)

        if args.type == "static" and not (d / "index.html").exists():
            return ToolResult(content=f"ERROR: static site requires index.html in {d}",
                              is_error=True)

        if args.package_only:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            zip_path = ctx.workspace.artifacts_dir / f"deploy-{args.type}-{ts}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in d.rglob("*"):
                    if not f.is_file():
                        continue
                    if any(p.startswith(".") and p not in (".", "..") for p in f.relative_to(d).parts):
                        continue  # скипаем .git, .next, .DS_Store и т.д.
                    if "node_modules" in f.parts:
                        continue
                    zf.write(f, f.relative_to(d))
            size_mb = zip_path.stat().st_size / 1024 / 1024
            return ToolResult(
                content=(f"OK: packaged {args.type} from {d} → {zip_path} ({size_mb:.2f} MB).\n"
                         "NOTE: real deployment to public production env is not implemented in MVP. "
                         "Send the zip to user via message_notify_user with attachments."),
                artifacts=[str(zip_path)],
                metadata={"zip": str(zip_path), "size_mb": size_mb, "type": args.type},
            )

        return ToolResult(
            content="ERROR: package_only=False is not implemented (no provider connected).",
            is_error=True,
        )


def make_deploy_tools() -> list[Tool]:
    return [DeployExposeTool(), DeployApplyTool()]
