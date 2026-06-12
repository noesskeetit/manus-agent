"""Конфигурация Manus-агента: модели, пути, секреты."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _load_env_files() -> None:
    """Подтягивает переменные из ./.env и ~/.config/manus/secrets.env."""
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".config" / "manus" / "secrets.env",
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)


_load_env_files()


# ---------- Модели ----------

@dataclass(frozen=True)
class ModelSpec:
    """Описание одной модели — id, провайдер, контекст, фичи."""
    id: str                            # cloudru/Qwen/Qwen3-Coder-Next
    short: str                         # kimi26
    api_base: str
    api_key_env: str                   # имя env-переменной с ключом
    context_window: int                # макс tokens
    supports_tool_calling: bool = True
    notes: str = ""


# Cloud.ru FM API (foundation models)
_CLOUDRU_BASE = os.environ.get(
    "MANUS_CLOUDRU_BASE",
    "https://foundation-models.api.cloud.ru/v1",
)


MODELS: dict[str, ModelSpec] = {
    "minimax": ModelSpec(
        id="MiniMaxAI/MiniMax-M2",
        short="minimax",
        api_base=_CLOUDRU_BASE,
        api_key_env="LLM_API_KEY",
        context_window=192_000,
        supports_tool_calling=True,
        notes="Native tool_calling, хорош как planner. Возвращает много reasoning_content.",
    ),
    "glm": ModelSpec(
        id="zai-org/GLM-4.7",
        short="glm",
        api_base=_CLOUDRU_BASE,
        api_key_env="LLM_API_KEY",
        context_window=200_000,
        supports_tool_calling=False,  # кладёт <tool_call> XML в thinking — не подходит
        notes="Хорош для compaction/summary (без tool_calling).",
    ),
    "minimax25": ModelSpec(
        id="MiniMaxAI/MiniMax-M2.5",
        short="minimax25",
        api_base=_CLOUDRU_BASE,
        api_key_env="LLM_API_KEY",
        context_window=196_608,
        supports_tool_calling=True,
        notes="Cloud.ru FM API MiniMax M2.5 agent/tool-use profile.",
    ),
    "kimi26": ModelSpec(
        id="moonshotai/Kimi-K2.6",
        short="kimi26",
        api_base=_CLOUDRU_BASE,
        api_key_env="LLM_API_KEY",
        context_window=262_144,
        supports_tool_calling=True,
        notes="Cloud.ru FM API Kimi K2.6 profile for long-context executor workers.",
    ),
    "deepseek-v4-pro": ModelSpec(
        id="deepseek-ai/DeepSeek-V4-Pro",
        short="deepseek-v4-pro",
        api_base=_CLOUDRU_BASE,
        api_key_env="LLM_API_KEY",
        context_window=262_144,
        supports_tool_calling=True,
        notes="Cloud.ru FM API DeepSeek V4 Pro profile for reasoning-heavy workers.",
    ),
    "glm51": ModelSpec(
        id="zai-org/GLM-5.1",
        short="glm51",
        api_base=_CLOUDRU_BASE,
        api_key_env="LLM_API_KEY",
        context_window=262_144,
        supports_tool_calling=True,
        notes="Cloud.ru FM API GLM 5.1 profile for executor or summarizer workers.",
    ),
}


def get_model(short: str) -> ModelSpec:
    if short not in MODELS:
        raise ValueError(f"Unknown model '{short}'. Available: {list(MODELS)}")
    return MODELS[short]


# ---------- Пути ----------

def _path_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def _manus_home() -> Path:
    return _path_env("MANUS_HOME", Path.home() / "manus")


def _manus_workspaces() -> Path:
    return _path_env("MANUS_WORKSPACE_ROOT", _manus_home() / "workspace")


def _manus_secrets() -> Path:
    return _path_env("MANUS_SECRETS_PATH", Path.home() / ".config" / "manus" / "secrets.env")


def _manus_log_dir() -> Path:
    return _path_env("MANUS_LOG_DIR", _manus_home() / "logs")


@dataclass
class Paths:
    home: Path = field(default_factory=_manus_home)
    workspaces: Path = field(default_factory=_manus_workspaces)
    secrets: Path = field(default_factory=_manus_secrets)
    log_dir: Path = field(default_factory=_manus_log_dir)

    def ensure(self) -> None:
        for p in [self.home, self.workspaces, self.log_dir, self.secrets.parent]:
            p.mkdir(parents=True, exist_ok=True)


PATHS = Paths()


# ---------- Глобальные настройки агента ----------

@dataclass
class AgentConfig:
    # Модели по ролям
    executor_model: str = "kimi26"          # делает работу, native tool_calling
    planner_model: str = "minimax"          # для длинных задач (>3h)
    summarizer_model: str = "glm"           # сжимает старые turns

    # Контекст: 5-layer compaction (Anthropic Claude Code pipeline)
    # Каждый stage triggers когда est_tokens > его threshold
    snip_pct: float = 0.65                  # 1: head+tail длинные tool_results in-place (no LLM)
    microcompact_pct: float = 0.75          # 2: старые turns → 1 строка каждый (LLM)
    block_summary_pct: float = 0.80         # 3: блочные summaries (текущая логика)
    meta_collapse_pct: float = 0.85         # 4: блок-summaries → meta-summary
    auto_compact_pct: float = 0.92          # 5: last resort — system + last 5 + meta
    max_context_pct: float = 0.75           # legacy совместимость (== microcompact_pct трейтер)
    keep_last_turns_raw: int = 12           # сколько последних turn'ов всегда оставлять необжатыми
    big_observation_threshold: int = 2000   # обзёрвейшны больше — на диск, в контекст путь+TL;DR
    snip_keep_head: int = 500               # символов сверху сохранять в snip stage
    snip_keep_tail: int = 300               # символов снизу
    snip_min_size: int = 2000               # tool_result короче — не snip'аем

    # Tool execution
    tool_call_timeout_sec: int = 120        # дефолтный timeout
    tool_retry_max_attempts: int = 3
    tool_retry_backoff_base: float = 1.0    # 1s → 2s → 4s

    # LLM
    llm_temperature: float = 0.4            # умеренный для агентных задач
    llm_max_tokens_per_turn: int = 30_000
    llm_request_timeout_sec: int = 180
    llm_retry_max_attempts: int = 5         # сетевые ошибки

    # Длительные задачи
    planner_executor_threshold_hours: float = 3.0  # >3h → planner-executor mode
    max_iterations: int = 500               # safety-предохранитель против бесконечных циклов
    auto_compact_min_turns: int = 20        # не компактируем если turns мало

    # Stuck detection (по OpenHands)
    stuck_action_observation_threshold: int = 4   # 4+ повторов action→observation
    stuck_action_error_threshold: int = 3         # 3+ одинаковых ошибок
    stuck_monologue_threshold: int = 3            # 3+ ответов без tool_call
    stuck_no_progress_iter: int = 15              # todo.md не меняется N итераций

    # Cost / safety ceiling
    max_total_tokens: int = 2_000_000             # hard limit на токены за всю сессию
    max_session_seconds: int = 4 * 3600           # 4 часа максимум на одну сессию

    # Sub-agents
    subagent_inherit_workspace: bool = True
    subagent_max_concurrent: int = 4

    # Telegram (опционально)
    tg_bot_token: Optional[str] = field(default_factory=lambda: os.environ.get("MANUS_TG_BOT_TOKEN"))
    tg_user_id: Optional[str] = field(default_factory=lambda: os.environ.get("MANUS_TG_USER_ID"))

    @property
    def tg_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_user_id)


CONFIG = AgentConfig()
