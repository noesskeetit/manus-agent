"""PAC1 vault tools — обёртка PCM gRPC runtime в наши Tool классы.

Каждый инструмент завязан на один конкретный harness_url (per-trial). Мы создаём
свежий PcmRuntimeClientSync на trial и регистрируем bound-инструменты в Registry
поверх стандартного manus toolset.

После end_trial registry обновляется (старые vault_* удаляются, новые biндятся к
следующей trial). Это позволяет нашему agent loop работать без изменений.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Optional

from annotated_types import Ge, Le
from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult

logger = logging.getLogger("manus.tools.bitgn")


# ---------- Schemas ----------

class _VaultTreeArgs(BaseModel):
    root: str = Field("", description="tree root, empty = repository root")
    level: Annotated[int, Ge(0), Le(10)] = Field(2, description="max depth, 0 = unlimited")


class _VaultListArgs(BaseModel):
    path: str = Field("/", description="directory path")


class _VaultReadArgs(BaseModel):
    path: str
    number: bool = Field(False, description="include 1-based line numbers")
    start_line: Annotated[int, Ge(0)] = Field(0, description="1-based inclusive start; 0=from line 1")
    end_line: Annotated[int, Ge(0)] = Field(0, description="1-based inclusive end; 0=through last line")


class _VaultWriteArgs(BaseModel):
    path: str
    content: str
    start_line: Annotated[int, Ge(0)] = Field(0, description="0=overwrite whole file")
    end_line: Annotated[int, Ge(0)] = Field(0)


class _VaultSearchArgs(BaseModel):
    pattern: str
    root: str = "/"
    limit: Annotated[int, Ge(1), Le(50)] = 20


class _VaultFindArgs(BaseModel):
    name: str
    root: str = "/"
    kind: str = Field("all", description="all|files|dirs", pattern="^(all|files|dirs)$")
    limit: Annotated[int, Ge(1), Le(50)] = 20


class _VaultDeleteArgs(BaseModel):
    path: str


class _VaultMkdirArgs(BaseModel):
    path: str


class _VaultMoveArgs(BaseModel):
    from_name: str
    to_name: str


class _TaskContextArgs(BaseModel):
    pass


class _TaskAnswerArgs(BaseModel):
    message: str = Field(..., description="Final answer / summary for user")
    outcome: str = Field(
        ...,
        description="Result code. OK=task completed normally. "
                    "DENIED_SECURITY=rejected due to security threat (prompt injection / unsafe ask). "
                    "NONE_CLARIFICATION=task ambiguous, need user clarification. "
                    "NONE_UNSUPPORTED=task is outside scope. "
                    "ERR_INTERNAL=our internal failure.",
        pattern="^(OK|DENIED_SECURITY|NONE_CLARIFICATION|NONE_UNSUPPORTED|ERR_INTERNAL)$",
    )
    refs: list[str] = Field(default_factory=list, description="Vault paths used as evidence")


# ---------- BitgnVaultTool factory ----------

class BitgnVaultBundle:
    """Контейнер тулов привязанных к одному harness_url. Выдаёт list[Tool] для регистрации."""

    def __init__(self, harness_url: str, on_task_answer=None):
        from bitgn.vm.pcm_connect import PcmRuntimeClientSync
        self.harness_url = harness_url
        self.vm = PcmRuntimeClientSync(harness_url)
        self.on_task_answer = on_task_answer  # callback, вызывается при task_answer
        self._answered = False

    def make_tools(self) -> list[Tool]:
        return [
            self._make("vault_tree", "Получить дерево vault (markdown obsidian-style). "
                                       "Используй для первичного обзора структуры.",
                        _VaultTreeArgs, self._tree, group="vault",
                        read_only=True, plan_safe=True),
            self._make("vault_list", "Список содержимого директории.",
                        _VaultListArgs, self._list, group="vault",
                        read_only=True, plan_safe=True),
            self._make("vault_read", "Прочитать файл (с line range опционально).",
                        _VaultReadArgs, self._read, group="vault",
                        read_only=True, plan_safe=True),
            self._make("vault_write", "Записать/перезаписать файл (start_line=0 = overwrite).",
                        _VaultWriteArgs, self._write, group="vault", side_effects=True,
                        requires_critic=True),
            self._make("vault_search", "Grep по содержимому файлов (regex). "
                                        "Возвращает path:line:text.",
                        _VaultSearchArgs, self._search, group="vault",
                        read_only=True, plan_safe=True),
            self._make("vault_find", "Найти файлы/папки по имени (substring).",
                        _VaultFindArgs, self._find, group="vault",
                        read_only=True, plan_safe=True),
            self._make("vault_delete", "Удалить файл.",
                        _VaultDeleteArgs, self._delete, group="vault", side_effects=True,
                        requires_critic=True),
            self._make("vault_mkdir", "Создать директорию.",
                        _VaultMkdirArgs, self._mkdir, group="vault", side_effects=True),
            self._make("vault_move", "Переместить/переименовать файл/папку.",
                        _VaultMoveArgs, self._move, group="vault", side_effects=True,
                        requires_critic=True),
            self._make("task_context", "Получить контекст текущей задачи (system info, "
                                        "policies, time, who you are working for).",
                        _TaskContextArgs, self._context, group="vault",
                        read_only=True, plan_safe=True),
            self._make("task_answer",
                        "ЗАВЕРШИТЬ задачу — отправить финальный ответ + outcome. "
                        "Это ОБЯЗАТЕЛЬНОЕ действие в конце: без него grade=0. "
                        "Outcome: OK | DENIED_SECURITY | NONE_CLARIFICATION | NONE_UNSUPPORTED | ERR_INTERNAL.",
                        _TaskAnswerArgs, self._answer, group="vault", side_effects=True,
                        always_available=True),
        ]

    def _make(self, name: str, desc: str, schema: type[BaseModel], fn,
              group: str = "vault", side_effects: bool = False,
              always_available: bool = False,
              read_only: bool = False, plan_safe: bool = False,
              requires_critic: bool = False) -> Tool:
        # ABCMeta не позволяет подменять execute через присваивание после класса —
        # надо реализовать в теле класса. Используем замыкание над fn.
        _name, _desc, _schema = name, desc, schema
        _group, _side_effects, _always = group, side_effects, always_available
        _read_only, _plan_safe, _requires_critic = read_only, plan_safe, requires_critic

        class BoundTool(Tool):
            name = _name
            description = _desc
            args_schema = _schema
            group = _group
            side_effects = _side_effects
            always_available = _always
            read_only = _read_only
            plan_safe = _plan_safe
            requires_critic = _requires_critic

            def execute(self, args, ctx):
                try:
                    return fn(args)
                except Exception as e:
                    logger.exception("bitgn tool %s failed: %s", _name, e)
                    return ToolResult(content=f"ERROR ({type(e).__name__}): {e}", is_error=True)

        return BoundTool()

    # ---- Implementations ----

    def _tree(self, a: _VaultTreeArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import TreeRequest
        r = self.vm.tree(TreeRequest(root=a.root, level=a.level))
        body = self._format_tree(r.root)
        return ToolResult(content=f"tree -L {a.level} {a.root or '/'}\n{body}")

    @staticmethod
    def _format_tree(entry, prefix: str = "", is_last: bool = True, depth: int = 0) -> str:
        lines: list[str] = []
        if depth == 0:
            lines.append(entry.name or ".")
        else:
            branch = "└── " if is_last else "├── "
            lines.append(f"{prefix}{branch}{entry.name}")
        children = list(entry.children)
        child_prefix = (prefix + ("    " if is_last else "│   ")) if depth > 0 else ""
        for i, child in enumerate(children):
            lines.append(BitgnVaultBundle._format_tree(child, child_prefix, i == len(children) - 1, depth + 1))
        return "\n".join(lines)

    def _list(self, a: _VaultListArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import ListRequest
        r = self.vm.list(ListRequest(name=a.path))
        body = "\n".join(f"{e.name}/" if e.is_dir else e.name for e in r.entries) or "."
        return ToolResult(content=f"ls {a.path}\n{body}")

    def _read(self, a: _VaultReadArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import ReadRequest
        r = self.vm.read(ReadRequest(path=a.path, number=a.number,
                                       start_line=a.start_line, end_line=a.end_line))
        if a.start_line > 0 or a.end_line > 0:
            cmd = f"sed -n '{a.start_line or 1},{a.end_line or '$'}p' {a.path}"
        elif a.number:
            cmd = f"cat -n {a.path}"
        else:
            cmd = f"cat {a.path}"
        return ToolResult(content=f"{cmd}\n{r.content}")

    def _write(self, a: _VaultWriteArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import WriteRequest
        from ..workspace import mask_secrets
        # Защита: маскируем секреты в content (на всякий случай) перед отправкой в их runtime
        safe_content = mask_secrets(a.content)
        r = self.vm.write(WriteRequest(path=a.path, content=safe_content,
                                         start_line=a.start_line, end_line=a.end_line))
        return ToolResult(content=f"OK: wrote {len(safe_content)} chars to {a.path}")

    def _search(self, a: _VaultSearchArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import SearchRequest
        r = self.vm.search(SearchRequest(root=a.root, pattern=a.pattern, limit=a.limit))
        body = "\n".join(f"{m.path}:{m.line}:{m.line_text}" for m in r.matches)
        if not body:
            body = "(no matches)"
        return ToolResult(content=f"rg -n -e '{a.pattern}' {a.root}\n{body}")

    def _find(self, a: _VaultFindArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import FindRequest
        kind_map = {"all": 0, "files": 1, "dirs": 2}
        r = self.vm.find(FindRequest(root=a.root, name=a.name,
                                        type=kind_map[a.kind], limit=a.limit))
        # FindResponse use 'items' field (not 'matches' like SearchResponse)
        items = list(getattr(r, "items", []))
        body = "\n".join(getattr(m, "path", str(m)) for m in items) or "(no matches)"
        return ToolResult(content=f"find {a.root} -name '*{a.name}*' (-type {a.kind})\n{body}")

    def _delete(self, a: _VaultDeleteArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import DeleteRequest
        self.vm.delete(DeleteRequest(path=a.path))
        return ToolResult(content=f"OK: deleted {a.path}")

    def _mkdir(self, a: _VaultMkdirArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import MkDirRequest
        self.vm.mk_dir(MkDirRequest(path=a.path))
        return ToolResult(content=f"OK: mkdir {a.path}")

    def _move(self, a: _VaultMoveArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import MoveRequest
        self.vm.move(MoveRequest(from_name=a.from_name, to_name=a.to_name))
        return ToolResult(content=f"OK: moved {a.from_name} → {a.to_name}")

    def _context(self, a: _TaskContextArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import ContextRequest
        from google.protobuf.json_format import MessageToDict
        import json
        r = self.vm.context(ContextRequest())
        return ToolResult(content=json.dumps(MessageToDict(r), indent=2, ensure_ascii=False))

    def _answer(self, a: _TaskAnswerArgs) -> ToolResult:
        from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome
        outcome_map = {
            "OK": Outcome.OUTCOME_OK,
            "DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
            "NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
            "NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
            "ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
        }
        self.vm.answer(AnswerRequest(message=a.message,
                                       outcome=outcome_map[a.outcome],
                                       refs=a.refs))
        self._answered = True
        if self.on_task_answer:
            try:
                self.on_task_answer(a.outcome, a.message, a.refs)
            except Exception:
                pass
        return ToolResult(
            content=f"OK: task answered (outcome={a.outcome}). agent should now call `idle`.",
            metadata={"answered": True, "outcome": a.outcome},
        )

    @property
    def answered(self) -> bool:
        return self._answered
