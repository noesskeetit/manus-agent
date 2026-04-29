"""Все стандартные тулы агента."""
from __future__ import annotations

from .base import Tool, ToolRegistry, ToolContext, ToolResult
from .file_ops import make_file_tools
from .shell import make_shell_tools
from .search import make_search_tools
from .message import make_message_tools
from .memory_tools import make_memory_tools
from .idle import make_idle_tools
from .deploy import make_deploy_tools
from .image import make_image_tools
from .code import make_code_tools
from .todo import make_todo_tools
from .skills_tool import make_skills_tools
from .lifecycle import make_lifecycle_tools


def build_default_registry() -> ToolRegistry:
    """Собрать стандартный набор инструментов агента."""
    reg = ToolRegistry()
    reg.register_many(make_file_tools())
    reg.register_many(make_shell_tools())
    reg.register_many(make_search_tools())
    reg.register_many(make_message_tools())
    reg.register_many(make_memory_tools())
    reg.register_many(make_idle_tools())
    reg.register_many(make_deploy_tools())
    reg.register_many(make_image_tools())
    reg.register_many(make_code_tools())
    reg.register_many(make_todo_tools())
    reg.register_many(make_skills_tools())
    reg.register_many(make_lifecycle_tools())
    # Browser tools — lazy import (Playwright тяжёлый)
    try:
        from .browser import make_browser_tools
        reg.register_many(make_browser_tools())
    except Exception:
        pass
    # Sub-agent тул — lazy
    try:
        from .subagent_tool import make_subagent_tools
        reg.register_many(make_subagent_tools())
    except Exception:
        pass
    return reg


__all__ = [
    "Tool", "ToolRegistry", "ToolContext", "ToolResult",
    "build_default_registry",
]
