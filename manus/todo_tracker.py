"""Structured todo tracker — JSON store + auto-rendered markdown view (для UI compat).

Tasks have id, subject, status, blocks/blocked_by, parent. Atomic save через tmp+rename.
Регенерирует todo.md на каждом update — UI продолжает рендерить как раньше.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


@dataclass
class TaskItem:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    parent: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


class TaskStore:
    """JSON-backed task store with atomic writes + memory cache."""

    def __init__(self, path: Path):
        self.path = path
        self._cache: dict[str, TaskItem] | None = None

    def _load(self) -> dict[str, TaskItem]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = {tid: TaskItem(**td) for tid, td in data.items()}
        except (json.JSONDecodeError, TypeError, OSError):
            self._cache = {}
        return self._cache

    def _save(self) -> None:
        if self._cache is None:
            return
        data = {tid: asdict(t) for tid, t in self._cache.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        tmp.replace(self.path)

    def create(self, subject: str, description: str = "",
               parent: Optional[str] = None,
               blocked_by: Optional[list[str]] = None) -> TaskItem:
        tasks = self._load()
        tid = _new_id()
        t = TaskItem(
            id=tid, subject=subject, description=description,
            parent=parent, blocked_by=list(blocked_by or []),
        )
        tasks[tid] = t
        # Автоматически обновим blocks для referenced tasks
        for blocker_id in t.blocked_by:
            if blocker_id in tasks and tid not in tasks[blocker_id].blocks:
                tasks[blocker_id].blocks.append(tid)
                tasks[blocker_id].updated_at = _now_iso()
        self._save()
        return t

    def update(self, tid: str, status: Optional[str] = None,
               subject: Optional[str] = None,
               description: Optional[str] = None,
               add_blocks: Optional[list[str]] = None,
               add_blocked_by: Optional[list[str]] = None,
               metadata: Optional[dict] = None) -> Optional[TaskItem]:
        tasks = self._load()
        if tid not in tasks:
            return None
        t = tasks[tid]
        if status is not None:
            t.status = status
        if subject is not None:
            t.subject = subject
        if description is not None:
            t.description = description
        if add_blocks:
            for bid in add_blocks:
                if bid in tasks and bid not in t.blocks:
                    t.blocks.append(bid)
                    if tid not in tasks[bid].blocked_by:
                        tasks[bid].blocked_by.append(tid)
                        tasks[bid].updated_at = _now_iso()
        if add_blocked_by:
            for bid in add_blocked_by:
                if bid in tasks and bid not in t.blocked_by:
                    t.blocked_by.append(bid)
                    if tid not in tasks[bid].blocks:
                        tasks[bid].blocks.append(tid)
                        tasks[bid].updated_at = _now_iso()
        if metadata:
            t.metadata.update(metadata)
        t.updated_at = _now_iso()
        self._save()
        return t

    def get(self, tid: str) -> Optional[TaskItem]:
        return self._load().get(tid)

    def list(self, status: Optional[str] = None,
             parent: Optional[str] = None) -> list[TaskItem]:
        tasks = self._load()
        out = list(tasks.values())
        if status:
            out = [t for t in out if t.status == status]
        if parent is not None:
            out = [t for t in out if t.parent == parent]
        return sorted(out, key=lambda t: t.created_at)

    def delete(self, tid: str) -> bool:
        tasks = self._load()
        if tid not in tasks:
            return False
        del tasks[tid]
        # Чистим refs
        for t in tasks.values():
            if tid in t.blocks:
                t.blocks.remove(tid)
            if tid in t.blocked_by:
                t.blocked_by.remove(tid)
        self._save()
        return True

    def all(self) -> dict[str, TaskItem]:
        return self._load()

    def render_markdown(self, task_text: str = "", task_id: str = "") -> str:
        """Auto-rendered todo.md view для UI / human reading."""
        tasks = self._load()
        if not tasks:
            return f"# Задача\n\n{task_text}\n\ntask_id: {task_id}\n\n_(no tasks tracked)_\n"
        # Собираем дерево по parent
        roots = [t for t in tasks.values() if not t.parent]
        roots.sort(key=lambda t: t.created_at)

        def status_box(s: str) -> str:
            return {
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]",
                "blocked": "[!]",
            }.get(s, "[?]")

        def render_task(t: TaskItem, depth: int = 0) -> list[str]:
            indent = "  " * depth
            lines = [f"{indent}- {status_box(t.status)} `{t.id}` **{t.subject}**"]
            if t.description:
                lines.append(f"{indent}  > {t.description[:200]}")
            if t.blocked_by:
                lines.append(f"{indent}  blocked_by: {', '.join(t.blocked_by)}")
            children = sorted([c for c in tasks.values() if c.parent == t.id],
                              key=lambda c: c.created_at)
            for ch in children:
                lines.extend(render_task(ch, depth + 1))
            return lines

        body_lines: list[str] = []
        for r in roots:
            body_lines.extend(render_task(r))
        out = (
            f"<!-- auto-generated from tasks.json — edit through todo_* tools -->\n"
            f"# Задача\n\n{task_text}\n\n"
            f"task_id: {task_id}\n\n"
            f"## Tasks ({len(tasks)} total, "
            f"{sum(1 for t in tasks.values() if t.status == 'completed')} done)\n\n"
            + "\n".join(body_lines)
            + "\n"
        )
        return out
