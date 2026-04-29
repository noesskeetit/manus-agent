"""Manus Cloud — autonomous AI agent."""
from __future__ import annotations

import logging

__version__ = "0.1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

for noisy in ("httpx", "httpcore", "openai._base_client", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


from .agent import Agent, AgentState, AgentPhase
from .workspace import Workspace, make_task_id
from .config import CONFIG, MODELS, get_model

__all__ = [
    "Agent", "AgentState", "AgentPhase",
    "Workspace", "make_task_id",
    "CONFIG", "MODELS", "get_model",
]
