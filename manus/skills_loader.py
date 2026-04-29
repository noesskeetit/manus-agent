"""Skills 3-level progressive disclosure (Claude Code / Manus 2026 паттерн).

Структура:
    manus/skills/<name>/
    ├── SKILL.md         # frontmatter (tier-1) + instructions (tier-2)
    └── resources/       # tier-3 lazy via file_read
        └── ...

Frontmatter (tier-1, всегда показывается агенту в metadata block):
---
name: research
description: One-line that triggers attention
triggers: [keyword1, keyword2, ...]
active_groups: [research, file, memory, lifecycle, communication]
allowed_role: researcher
version: 1
---

Tier-2 (instructions, ~3-5K tok) — попадают в context при activation.
Tier-3 (resources/*.md) — lazy read через file_read когда нужно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SkillMetadata:
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    active_groups: Optional[list[str]] = None  # None = не меняем groups
    allowed_role: Optional[str] = None
    version: int = 1
    file_path: Path = field(default_factory=lambda: Path("/dev/null"))

    @property
    def short_metadata(self) -> str:
        """Tier-1 краткое описание для system prompt (~50-80 tok)."""
        return f"`{self.name}`: {self.description}"


@dataclass
class Skill:
    metadata: SkillMetadata
    instructions: str  # tier-2 content


# ---------- Parser ----------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Naive YAML-like parser (без full YAML — у нас простые типы)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    fm: dict = {}
    current_key = None
    for line in fm_text.split("\n"):
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("-"):
            # list item continuation
            if current_key is not None:
                val = line.lstrip("- ").strip()
                if not isinstance(fm.get(current_key), list):
                    fm[current_key] = []
                fm[current_key].append(val)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        current_key = key
        if not value:
            fm[key] = None
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fm[key] = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
        elif value.lower() in ("true", "false"):
            fm[key] = value.lower() == "true"
        elif value.isdigit():
            fm[key] = int(value)
        else:
            fm[key] = value.strip('"').strip("'")
    return fm, body


def parse_skill(skill_md_path: Path) -> Optional[Skill]:
    if not skill_md_path.exists():
        return None
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = _parse_frontmatter(text)
    if not fm.get("name"):
        return None
    md = SkillMetadata(
        name=fm.get("name", skill_md_path.parent.name),
        description=fm.get("description", "(no description)"),
        triggers=fm.get("triggers") or [],
        active_groups=fm.get("active_groups"),
        allowed_role=fm.get("allowed_role"),
        version=fm.get("version", 1) if isinstance(fm.get("version"), int) else 1,
        file_path=skill_md_path,
    )
    return Skill(metadata=md, instructions=body)


def discover_skills(skills_dir: Optional[Path] = None) -> dict[str, Skill]:
    """Найти все SKILL.md файлы в директории (immediate subdirs)."""
    if skills_dir is None:
        skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.exists():
        return {}
    skills: dict[str, Skill] = {}
    for d in skills_dir.iterdir():
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        skill = parse_skill(skill_md)
        if skill:
            skills[skill.metadata.name] = skill
    return skills


# ---------- Trigger matching (для auto-suggest активаций) ----------

def detect_relevant_skills(task_text: str,
                            skills: Optional[dict[str, Skill]] = None) -> list[str]:
    """Имена skills чьи triggers матчатся в task_text."""
    if skills is None:
        skills = discover_skills()
    text_lower = task_text.lower()
    out: list[str] = []
    for name, skill in skills.items():
        for trigger in skill.metadata.triggers:
            if trigger.lower() in text_lower:
                out.append(name)
                break
    return out
