"""Sub-agents для распараллеленных подзадач (исследование, анализ).

C1: async pool с polling-based completion notification.

По советам Cognition ("Don't Build Multi-Agents") — sub-agents используем ТОЛЬКО для:
- Read-only investigation (research, analysis, document review)
- Параллельный fan-out по независимым темам (research 5 разных продуктов)
- НЕ для actions с побочкой (publish, deploy, write files в shared paths)

Контракт:
- Sub-agent работает в SVOEM workspace `~/manus/workspace/<parent_id>/sub/<sub_id>/`
- Получает task_text + scope ограничения + допустимые тулы
- Возвращает structured summary через output.json (НЕ модифицирует parent's todo.md)
- Изоляция через subprocess (не threading) — crash isolation
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import CONFIG, PATHS

logger = logging.getLogger("manus.subagent")


@dataclass
class SubAgentResult:
    sub_id: str
    status: str                      # "completed" | "failed" | "timeout"
    summary: str
    artifacts: list[str] = field(default_factory=list)
    findings: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_sec: float = 0.0
    workspace_path: Optional[str] = None


def _write_input(parent_workspace_path: Path, sub_id: str,
                 task: str, scope: dict, model: str,
                 max_iterations: int, allowed_tools: Optional[list[str]],
                 role: Optional[str] = None,
                 active_groups: Optional[list[str]] = None,
                 parent_recursion_depth: int = 0) -> Path:
    sub_root = parent_workspace_path / "sub" / sub_id
    sub_root.mkdir(parents=True, exist_ok=True)
    inp = sub_root / "input.json"
    inp.write_text(json.dumps({
        "task": task,
        "scope": scope,
        "model": model,
        "max_iterations": max_iterations,
        "allowed_tools": allowed_tools,
        "parent_workspace": str(parent_workspace_path),
        "sub_id": sub_id,
        "role": role,
        "active_groups": active_groups,
        "recursion_depth": parent_recursion_depth + 1,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return sub_root


def _runner_script() -> Path:
    return Path(__file__).parent / "_subagent_runner.py"


def spawn_subagent(
    parent_workspace_path: str | Path,
    task: str,
    scope: Optional[dict] = None,
    model: Optional[str] = None,
    max_iterations: int = 50,
    allowed_tools: Optional[list[str]] = None,
    timeout_sec: int = 1800,
    role: Optional[str] = None,
    active_groups: Optional[list[str]] = None,
    max_recursion_depth: int = 2,
) -> SubAgentResult:
    """Запустить одного sub-agent'а (blocking).

    `role`: 'planner'|'executor'|'researcher'|'critic'|'debugger' (см. prompts/roles/<role>.md)
    `active_groups`: явное переопределение (если None — берётся из role default или generic)
    `max_recursion_depth`: limit вложенности sub-agents
    """
    # Recursion guard: проверяем глубину parent'а через env var (set'ит runner)
    parent_depth = int(os.environ.get("MANUS_SUBAGENT_RECURSION_DEPTH", "0"))
    if parent_depth >= max_recursion_depth:
        return SubAgentResult(
            sub_id="", status="failed",
            summary=f"Recursion limit reached (depth={parent_depth}, max={max_recursion_depth})",
            error="recursion_limit",
        )

    sub_id = uuid.uuid4().hex[:8]
    sub_root = _write_input(
        Path(parent_workspace_path), sub_id, task, scope or {},
        model or CONFIG.executor_model, max_iterations, allowed_tools,
        role=role, active_groups=active_groups,
        parent_recursion_depth=parent_depth,
    )

    runner = _runner_script()
    if not runner.exists():
        raise RuntimeError(f"Sub-agent runner script not found: {runner}")

    log_file = sub_root / "subagent.log"
    t0 = time.monotonic()
    try:
        with log_file.open("w", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                [sys.executable, str(runner), str(sub_root)],
                stdout=logf, stderr=subprocess.STDOUT,
                env={**os.environ, "MANUS_SUBAGENT": "1"},
            )
            try:
                rc = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                duration = time.monotonic() - t0
                return SubAgentResult(
                    sub_id=sub_id, status="timeout",
                    summary=f"Sub-agent timed out after {timeout_sec}s",
                    error="timeout", duration_sec=duration,
                    workspace_path=str(sub_root),
                )
    except Exception as e:
        return SubAgentResult(
            sub_id=sub_id, status="failed",
            summary=f"Sub-agent failed to start: {e}",
            error=str(e), duration_sec=time.monotonic() - t0,
            workspace_path=str(sub_root),
        )

    duration = time.monotonic() - t0
    out_file = sub_root / "output.json"
    if not out_file.exists():
        # Sub-agent крэшнулся не дописав output. Возьмём журнал.
        log_tail = ""
        if log_file.exists():
            try:
                log_tail = log_file.read_text(encoding="utf-8")[-3000:]
            except Exception:
                pass
        return SubAgentResult(
            sub_id=sub_id, status="failed",
            summary=f"Sub-agent exited rc={rc} without writing output. Log tail:\n{log_tail}",
            error=f"no output, rc={rc}", duration_sec=duration,
            workspace_path=str(sub_root),
        )

    try:
        data = json.loads(out_file.read_text(encoding="utf-8"))
    except Exception as e:
        return SubAgentResult(
            sub_id=sub_id, status="failed",
            summary=f"Sub-agent output unparseable: {e}",
            error=str(e), duration_sec=duration,
            workspace_path=str(sub_root),
        )

    return SubAgentResult(
        sub_id=sub_id,
        status=data.get("status", "completed"),
        summary=data.get("summary", ""),
        artifacts=data.get("artifacts", []),
        findings=data.get("findings", {}),
        error=data.get("error"),
        duration_sec=duration,
        workspace_path=str(sub_root),
    )


# ---------- C1: Async pool ----------

_ATEXIT_REGISTERED = False
_ALL_ASYNC_PROCS: list[subprocess.Popen] = []


def _atexit_kill_all():
    """Kill всех async children при выходе main agent process."""
    import os as _os
    if _os.environ.get("MANUS_KEEP_ASYNC_CHILDREN", "").lower() in ("1", "true", "yes"):
        return
    for p in _ALL_ASYNC_PROCS:
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
        except Exception:
            pass


def spawn_subagent_async(
    parent_workspace_path: str | Path,
    task: str,
    scope: Optional[dict] = None,
    model: Optional[str] = None,
    max_iterations: int = 50,
    timeout_sec: int = 1800,
    role: Optional[str] = None,
    active_groups: Optional[list[str]] = None,
) -> dict:
    """Spawn sub-agent НЕ блокируя main. Возвращает dict с sub_id, output_path, pid.

    main agent периодически poll'ит output_path для completion.
    """
    global _ATEXIT_REGISTERED
    parent_depth = int(os.environ.get("MANUS_SUBAGENT_RECURSION_DEPTH", "0"))
    sub_id = uuid.uuid4().hex[:8]
    sub_root = _write_input(
        Path(parent_workspace_path), sub_id, task, scope or {},
        model or CONFIG.executor_model, max_iterations, None,
        role=role, active_groups=active_groups,
        parent_recursion_depth=parent_depth,
    )
    runner = _runner_script()
    log_file = sub_root / "subagent.log"
    logf = log_file.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(sub_root)],
        stdout=logf, stderr=subprocess.STDOUT,
        env={**os.environ, "MANUS_SUBAGENT": "1",
             "MANUS_SUBAGENT_RECURSION_DEPTH": str(parent_depth + 1)},
    )
    _ALL_ASYNC_PROCS.append(proc)
    if not _ATEXIT_REGISTERED:
        import atexit as _atexit
        _atexit.register(_atexit_kill_all)
        _ATEXIT_REGISTERED = True

    started_at = time.time()
    return {
        "sub_id": sub_id,
        "output_path": str(sub_root / "output.json"),
        "log_path": str(log_file),
        "workspace_path": str(sub_root),
        "pid": proc.pid,
        "started_at": started_at,
        "timeout_at": started_at + timeout_sec,
    }


def check_async_subagent(info: dict) -> Optional[SubAgentResult]:
    """Если sub-agent finished (output.json existует и valid) — возвращаем result.
    Иначе None (still running). Если timeout — kill + failed result.
    """
    out_path = Path(info["output_path"])
    if out_path.exists():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            return SubAgentResult(
                sub_id=info["sub_id"],
                status=data.get("status", "completed"),
                summary=data.get("summary", ""),
                artifacts=data.get("artifacts", []),
                findings=data.get("findings", {}),
                error=data.get("error"),
                duration_sec=time.time() - info["started_at"],
                workspace_path=info["workspace_path"],
            )
        except Exception:
            return None
    # Timeout check
    if time.time() > info.get("timeout_at", float("inf")):
        # Kill process
        for p in _ALL_ASYNC_PROCS:
            if p.pid == info["pid"]:
                try:
                    p.terminate()
                except Exception:
                    pass
                break
        return SubAgentResult(
            sub_id=info["sub_id"],
            status="timeout",
            summary=f"async sub-agent timed out (sub_id={info['sub_id']})",
            error="timeout",
            duration_sec=time.time() - info["started_at"],
            workspace_path=info["workspace_path"],
        )
    return None


def spawn_many(
    parent_workspace_path: str | Path,
    tasks: list[dict],
    max_concurrent: int = 4,
    model: Optional[str] = None,
    max_iterations: int = 40,
    timeout_sec: int = 1800,
) -> list[SubAgentResult]:
    """Запустить несколько sub-agents параллельно. tasks: list of dict with 'task' + 'scope'."""
    max_concurrent = min(max_concurrent, CONFIG.subagent_max_concurrent, len(tasks)) or 1
    results: list[SubAgentResult] = []
    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futures: list[Future] = []
        for t in tasks:
            futures.append(ex.submit(
                spawn_subagent,
                parent_workspace_path,
                t.get("task", ""),
                t.get("scope"),
                model or t.get("model"),
                max_iterations,
                t.get("allowed_tools"),
                timeout_sec,
            ))
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:
                results.append(SubAgentResult(
                    sub_id="", status="failed",
                    summary=f"Future raised: {e}", error=str(e),
                ))
    return results
