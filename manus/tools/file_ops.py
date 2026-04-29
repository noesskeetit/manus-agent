"""Manus file_* эквиваленты: file_read, file_write, file_str_replace."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult


# ---------- Безопасность путей ----------

# Каталоги вне workspace, которые ОДНОЗНАЧНО запрещены даже на чтение.
# (Полный sandbox = только workspace; readonly здесь чуть мягче для UX.)
DENY_PREFIXES = (
    str(Path.home() / ".ssh"),
    str(Path.home() / ".aws"),
    str(Path.home() / ".config" / "manus"),
    str(Path.home() / ".gnupg"),
    "/etc/shadow",
    "/etc/sudoers",
    "/private/etc/shadow",
)


def _resolve_path(workspace_root: Path, raw: str) -> Path:
    """Принимаем абсолютный путь, либо относительный к workspace."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = workspace_root / p
    return p


def _is_denied(p: Path) -> bool:
    """Резолвит пути даже для несуществующих файлов (`..` сегменты обрабатываются)."""
    try:
        s = str(p.resolve(strict=False))
    except (OSError, RuntimeError):
        s = str(p.absolute())
    return any(s.startswith(prefix) for prefix in DENY_PREFIXES)


def _check_inside_workspace(workspace_root: Path, p: Path) -> bool:
    """True если path лежит внутри workspace (с резолвом symlinks)."""
    try:
        p_resolved = p.resolve(strict=False)
        ws_resolved = workspace_root.resolve(strict=False)
        p_resolved.relative_to(ws_resolved)
        return True
    except (ValueError, OSError):
        return False


# ---------- file_read ----------

class FileReadArgs(BaseModel):
    file: str = Field(..., description="Путь к файлу — абсолютный или относительный к workspace")
    start_line: int = Field(0, description="0-based starting line, отрицательные значения с конца")
    end_line: Optional[int] = Field(None, description="Exclusive end line. None = до конца")


class FileReadTool(Tool):
    group = "file"
    read_only = True
    plan_safe = True
    name = "file_read"
    description = ("Прочитать текстовый файл. Поддерживает срез по строкам. "
                   "Используй вместо shell `cat` — нет проблем с экранированием.")
    args_schema = FileReadArgs

    def execute(self, args: FileReadArgs, ctx: ToolContext) -> ToolResult:
        p = _resolve_path(ctx.workspace.root, args.file)
        if _is_denied(p):
            return ToolResult(
                content=f"ERROR: refusing to read sensitive path: {p}. "
                        "Edit DENY_PREFIXES in manus/tools/file_ops.py if you really need it.",
                is_error=True,
            )
        if not p.exists():
            return ToolResult(content=f"ERROR: file not found: {p}", is_error=True)
        if not p.is_file():
            return ToolResult(content=f"ERROR: not a file: {p}", is_error=True)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(content=f"ERROR: cannot read {p}: {e}", is_error=True)

        lines = text.splitlines()
        total = len(lines)
        s = args.start_line if args.start_line >= 0 else max(0, total + args.start_line)
        e = args.end_line if args.end_line is not None else total
        chunk = "\n".join(lines[s:e])
        header = f"[{p}] lines {s}-{min(e, total)} of {total}\n"
        return ToolResult(content=header + chunk,
                          metadata={"path": str(p), "total_lines": total})


# ---------- file_write ----------

class FileWriteArgs(BaseModel):
    file: str = Field(..., description="Путь — абсолютный или относительный к workspace")
    content: str = Field(..., description="Что записать")
    append: bool = Field(False, description="True = дописать в конец, False = перезаписать")
    leading_newline: bool = Field(False, description="Добавить \\n в начало (полезно при append)")
    trailing_newline: bool = Field(True, description="Добавить \\n в конец")


class FileWriteTool(Tool):
    group = "file"
    plan_safe = True   # OK для написания plan.md в plan mode
    name = "file_write"
    description = ("Записать текст в файл (overwrite/append). "
                   "Создаёт parent-директории. Используй вместо `echo > file`.")
    args_schema = FileWriteArgs
    side_effects = True

    def execute(self, args: FileWriteArgs, ctx: ToolContext) -> ToolResult:
        p = _resolve_path(ctx.workspace.root, args.file)
        # Безопасность: запись разрешена ТОЛЬКО в workspace.
        # Денни-листы перепроверяем явно (например ~/.config/manus → за пределами ws, всё равно блокируем).
        if _is_denied(p):
            return ToolResult(
                content=f"ERROR: refusing to write to sensitive path: {p}",
                is_error=True,
            )
        if not _check_inside_workspace(ctx.workspace.root, p):
            return ToolResult(
                content=(f"ERROR: refusing to write outside workspace ({p}). "
                         f"Workspace: {ctx.workspace.root}. "
                         "Use a path inside the workspace."),
                is_error=True,
            )

        p.parent.mkdir(parents=True, exist_ok=True)
        body = args.content
        if args.leading_newline and not body.startswith("\n"):
            body = "\n" + body
        if args.trailing_newline and not body.endswith("\n"):
            body = body + "\n"

        mode = "a" if args.append else "w"
        try:
            with p.open(mode, encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            return ToolResult(content=f"ERROR: write failed {p}: {e}", is_error=True)

        size = p.stat().st_size
        return ToolResult(
            content=f"OK: wrote {len(body)} chars ({size} bytes total) to {p} (mode={'append' if args.append else 'overwrite'})",
            artifacts=[str(p)],
            metadata={"path": str(p), "size": size},
        )


# ---------- file_str_replace ----------

class FileStrReplaceArgs(BaseModel):
    file: str = Field(..., description="Путь — абсолютный или относительный к workspace")
    old_str: str = Field(..., description="Точная подстрока для замены (должна встречаться 1 раз)")
    new_str: str = Field(..., description="Чем заменить")


class FileStrReplaceTool(Tool):
    group = "file"
    name = "file_str_replace"
    description = ("Заменить точную подстроку в файле (single-occurrence). "
                   "Используй для патчей todo.md, исправлений в коде. "
                   "Если old_str не уникален — расширь контекст вокруг.")
    args_schema = FileStrReplaceArgs
    side_effects = True

    def execute(self, args: FileStrReplaceArgs, ctx: ToolContext) -> ToolResult:
        p = _resolve_path(ctx.workspace.root, args.file)
        if _is_denied(p):
            return ToolResult(content=f"ERROR: refusing to modify sensitive path: {p}",
                              is_error=True)
        if not _check_inside_workspace(ctx.workspace.root, p):
            return ToolResult(
                content=(f"ERROR: refusing to modify outside workspace ({p}). "
                         f"Workspace: {ctx.workspace.root}."),
                is_error=True,
            )
        if not p.exists():
            return ToolResult(content=f"ERROR: file not found: {p}", is_error=True)
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(content=f"ERROR: cannot read {p}: {e}", is_error=True)

        count = txt.count(args.old_str)
        if count == 0:
            return ToolResult(
                content=(f"ERROR: old_str not found in {p}. "
                         f"Make sure it matches the file *exactly* (whitespace, newlines)."),
                is_error=True,
            )
        if count > 1:
            return ToolResult(
                content=(f"ERROR: old_str matches {count} times in {p}. "
                         "Expand it with more surrounding context to make it unique."),
                is_error=True,
            )

        new_txt = txt.replace(args.old_str, args.new_str, 1)
        p.write_text(new_txt, encoding="utf-8")
        return ToolResult(
            content=f"OK: replaced 1 occurrence in {p} (Δ {len(args.new_str) - len(args.old_str):+d} chars)",
            artifacts=[str(p)],
        )


# ---------- file_list ----------

class FileListArgs(BaseModel):
    path: str = Field(".", description="Путь — относительный к workspace или абсолютный")
    recursive: bool = Field(False, description="Рекурсивный обход")
    pattern: Optional[str] = Field(None, description="Glob-паттерн, например *.md")


class FileListTool(Tool):
    group = "file"
    read_only = True
    plan_safe = True
    name = "file_list"
    description = "Список файлов в директории (рекурсивный опционально, glob-фильтр опционально)"
    args_schema = FileListArgs

    def execute(self, args: FileListArgs, ctx: ToolContext) -> ToolResult:
        p = _resolve_path(ctx.workspace.root, args.path)
        if not p.exists():
            return ToolResult(content=f"ERROR: path not found: {p}", is_error=True)
        if not p.is_dir():
            return ToolResult(content=f"ERROR: not a directory: {p}", is_error=True)

        if args.recursive:
            it = p.rglob(args.pattern or "*")
        else:
            it = p.glob(args.pattern or "*")
        items = []
        for f in sorted(it):
            try:
                rel = f.relative_to(ctx.workspace.root)
            except ValueError:
                rel = f
            kind = "d" if f.is_dir() else "f"
            try:
                size = f.stat().st_size if f.is_file() else 0
            except Exception:
                size = 0
            items.append(f"{kind} {size:>10} {rel}")
            if len(items) >= 500:
                items.append(f"... (truncated, more files in {p})")
                break
        body = "\n".join(items) if items else "(empty)"
        return ToolResult(content=f"[{p}]\n{body}",
                          metadata={"count": len(items), "path": str(p)})


# ---------- file_search (grep over workspace) ----------

class FileSearchArgs(BaseModel):
    pattern: str = Field(..., description="Регулярка или подстрока для поиска в файлах")
    path: str = Field(".", description="Директория поиска (относительно workspace или абсолютная)")
    glob: Optional[str] = Field(None, description="Glob-фильтр имён файлов: *.py, *.md")
    max_hits: int = Field(50, description="Максимум совпадений")
    is_regex: bool = Field(False, description="Trait pattern as regex (else — substring)")


class FileSearchTool(Tool):
    group = "file"
    read_only = True
    plan_safe = True
    name = "file_search"
    description = (
        "Поиск pattern в текстовых файлах (grep аналог). "
        "Возвращает file:line — для удобной навигации к месту."
    )
    args_schema = FileSearchArgs

    def execute(self, args: FileSearchArgs, ctx: ToolContext) -> ToolResult:
        import re as _re
        root = _resolve_path(ctx.workspace.root, args.path)
        if _is_denied(root):
            return ToolResult(content=f"ERROR: refusing search in sensitive path: {root}",
                              is_error=True)
        if not root.exists():
            return ToolResult(content=f"ERROR: path not found: {root}", is_error=True)

        if args.is_regex:
            try:
                rgx = _re.compile(args.pattern, _re.IGNORECASE)
            except _re.error as e:
                return ToolResult(content=f"ERROR: invalid regex: {e}", is_error=True)
        else:
            rgx = _re.compile(_re.escape(args.pattern), _re.IGNORECASE)

        glob = args.glob or "*"
        hits: list[str] = []
        try:
            iterator = root.rglob(glob) if root.is_dir() else [root]
        except OSError:
            iterator = []
        for f in iterator:
            if not f.is_file():
                continue
            # Скипаем бинарники по размеру + некоторым расширениям
            if f.stat().st_size > 5_000_000:
                continue
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".pdf",
                                     ".zip", ".tar", ".gz", ".bin", ".so", ".dylib"):
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(txt.splitlines(), start=1):
                if rgx.search(line):
                    rel = f.relative_to(ctx.workspace.root) if str(f).startswith(str(ctx.workspace.root)) else f
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= args.max_hits:
                        break
            if len(hits) >= args.max_hits:
                break

        body = "\n".join(hits) if hits else f"(no matches for: {args.pattern})"
        return ToolResult(content=body, metadata={"hits": len(hits), "pattern": args.pattern})


def make_file_tools() -> list[Tool]:
    return [FileReadTool(), FileWriteTool(), FileStrReplaceTool(), FileListTool(), FileSearchTool()]
