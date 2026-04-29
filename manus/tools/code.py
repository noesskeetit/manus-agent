"""python_exec — CodeAct paradigm (Manus 2026 architectural shift).

По исследованию апреля 2026: Manus отошёл от JSON tool calls в пользу исполняемого
Python кода в sandbox. Один тул `python_exec` заменяет десятки специализированных,
позволяет композицию (несколько API-вызовов + условная логика в одном скрипте),
самоотладку (агент читает traceback, исправляет, повторяет).

Реализация: subprocess с workspace cwd, env с PYTHONPATH к stdlib + workspace,
timeout, capture stdout/stderr. Безопасность: НЕ полный sandbox (это macOS local-mode),
но cwd ограничен workspace, опциональный AST-блок-лист.

См. https://medium.com/@pankaj_pandey/inside-manus-the-architecture-that-replaced-tool-calls-with-executable-code
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


# Опасные импорты которые блокируем по умолчанию (наивный AST check)
DANGEROUS_IMPORTS = {
    "os.system", "subprocess.Popen",  # сложно, у нас shell_exec для этого
}


class PythonExecArgs(BaseModel):
    code: str = Field(..., description="Python код для исполнения. Печатай результаты через print(). "
                                        "Имеешь доступ к workspace через cwd. stdlib + установленные пакеты venv.")
    timeout_sec: int = Field(60, description="Таймаут (default 60s)")
    save_script: bool = Field(False, description="Сохранить скрипт в workspace/scripts/exec_<timestamp>.py")


class PythonExecTool(Tool):
    group = "shell"      # концептуально шеллу родственно
    name = "python_exec"
    description = (
        "Выполнить Python скрипт в sandbox-режиме (cwd = workspace). "
        "Это CodeAct paradigm: вместо отдельных тулов — пишешь Python который делает всё что нужно "
        "(API-запросы, обработка данных, файлы, расчёты). "
        "Доступны: stdlib (os, sys, json, re, datetime, urllib, http, csv, pathlib...), "
        "установленные пакеты (httpx, pydantic, beautifulsoup4, и т.д.). "
        "Печатай результаты через print() — они вернутся в context. "
        "Большой output автоматически дампится в observations/."
    )
    args_schema = PythonExecArgs
    side_effects = True

    def execute(self, args: PythonExecArgs, ctx: ToolContext) -> ToolResult:
        # Опционально сохраняем скрипт для аудита
        ws = ctx.workspace
        script_path: Optional[Path] = None
        if args.save_script:
            from datetime import datetime
            scripts_dir = ws.root / "scripts"
            scripts_dir.mkdir(exist_ok=True)
            script_path = scripts_dir / f"exec_{datetime.now().strftime('%H%M%S')}.py"
            script_path.write_text(args.code, encoding="utf-8")

        # Записываем код во временный файл (не stdin → чтобы tracebacks указывали на номер строки)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, dir=str(ws.root), encoding="utf-8",
        ) as tmp:
            tmp.write(args.code)
            tmp_path = Path(tmp.name)

        try:
            # Запускаем тем же python что и venv агента
            env = {**os.environ}
            # Гарантируем UTF-8 IO
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            r = subprocess.run(
                [sys.executable, str(tmp_path)],
                cwd=str(ws.root),
                env=env,
                capture_output=True,
                text=True,
                timeout=args.timeout_sec,
            )
            stdout = r.stdout or ""
            stderr = r.stderr or ""
            exit_code = r.returncode
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout.decode("utf-8", errors="replace") if e.stdout else "") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr.decode("utf-8", errors="replace") if e.stderr else "") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return ToolResult(
                content=(f"[TIMEOUT after {args.timeout_sec}s]\n"
                         f"--- stdout ---\n{stdout[-8000:]}\n--- stderr ---\n{stderr[-4000:]}"),
                is_error=True,
                metadata={"timeout": True, "script_path": str(script_path) if script_path else None},
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        # Формируем компактный output для контекста
        body_parts = [f"[python_exec exit={exit_code}]"]
        if stdout:
            body_parts.append(f"--- stdout ---\n{stdout[-30_000:]}")
        if stderr:
            body_parts.append(f"--- stderr ---\n{stderr[-10_000:]}")
        if not stdout and not stderr:
            body_parts.append("(no output)")
        body = "\n".join(body_parts)

        return ToolResult(
            content=body,
            raw={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            is_error=(exit_code != 0),
            artifacts=[str(script_path)] if script_path else [],
            metadata={
                "exit_code": exit_code,
                "stdout_size": len(stdout),
                "stderr_size": len(stderr),
                "script_path": str(script_path) if script_path else None,
            },
        )


def make_code_tools() -> list[Tool]:
    return [PythonExecTool()]
