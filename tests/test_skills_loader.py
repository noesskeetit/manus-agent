"""Unit-тесты для skills_loader — frontmatter parser, discover, trigger detection."""
from __future__ import annotations

from pathlib import Path

import pytest

from manus.skills_loader import (
    Skill,
    _parse_frontmatter,
    detect_relevant_skills,
    discover_skills,
    parse_skill,
)


# ---------- Frontmatter parser ----------

def test_parse_frontmatter_basic():
    text = """---
name: research
description: Deep research workflow
version: 1
---

Body content here.
"""
    fm, body = _parse_frontmatter(text)
    assert fm["name"] == "research"
    assert fm["description"] == "Deep research workflow"
    assert fm["version"] == 1
    assert "Body content" in body


def test_parse_frontmatter_inline_list():
    text = """---
name: x
triggers: [foo, bar, baz]
---

body
"""
    fm, _ = _parse_frontmatter(text)
    assert fm["triggers"] == ["foo", "bar", "baz"]


def test_parse_frontmatter_block_list():
    text = """---
name: x
triggers:
  - foo
  - bar
---

body
"""
    fm, _ = _parse_frontmatter(text)
    assert fm["triggers"] == ["foo", "bar"]


def test_parse_frontmatter_booleans():
    text = """---
name: x
enabled: true
verbose: false
---

body
"""
    fm, _ = _parse_frontmatter(text)
    assert fm["enabled"] is True
    assert fm["verbose"] is False


def test_parse_frontmatter_empty_returns_text_unchanged():
    text = "no frontmatter at all\nsecond line"
    fm, body = _parse_frontmatter(text)
    assert fm == {}
    assert body == text


# ---------- parse_skill ----------

def test_parse_skill_full(tmp_path):
    skill_dir = tmp_path / "research"
    skill_dir.mkdir()
    md = skill_dir / "SKILL.md"
    md.write_text("""---
name: research
description: Deep research workflow
triggers: [исследование, research]
active_groups: [research, file, memory]
allowed_role: researcher
version: 2
---

# Instructions

Do thorough research.
""", encoding="utf-8")
    skill = parse_skill(md)
    assert skill is not None
    assert skill.metadata.name == "research"
    assert skill.metadata.description == "Deep research workflow"
    assert "исследование" in skill.metadata.triggers
    assert skill.metadata.active_groups == ["research", "file", "memory"]
    assert skill.metadata.allowed_role == "researcher"
    assert skill.metadata.version == 2
    assert "Do thorough research." in skill.instructions


def test_parse_skill_missing_file_returns_none(tmp_path):
    assert parse_skill(tmp_path / "nope.md") is None


def test_parse_skill_no_name_returns_none(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_text("""---
description: only desc
---

body
""", encoding="utf-8")
    assert parse_skill(md) is None


def test_skill_short_metadata_format(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_text("""---
name: x
description: short desc
---

body
""", encoding="utf-8")
    skill = parse_skill(md)
    short = skill.metadata.short_metadata
    assert "x" in short
    assert "short desc" in short


# ---------- discover_skills ----------

def test_discover_skills_finds_all(tmp_path):
    for n in ["alpha", "beta"]:
        d = tmp_path / n
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: skill {n}\n---\n\nbody\n",
            encoding="utf-8",
        )
    skills = discover_skills(tmp_path)
    assert set(skills.keys()) == {"alpha", "beta"}


def test_discover_skills_skips_dirs_without_skill_md(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: a\n---\n\nbody",
        encoding="utf-8",
    )
    (tmp_path / "no_skill_dir").mkdir()  # без SKILL.md
    skills = discover_skills(tmp_path)
    assert "alpha" in skills
    assert "no_skill_dir" not in skills


def test_discover_skills_empty_dir(tmp_path):
    skills = discover_skills(tmp_path)
    assert skills == {}


def test_discover_skills_nonexistent_dir(tmp_path):
    skills = discover_skills(tmp_path / "doesnotexist")
    assert skills == {}


# ---------- detect_relevant_skills ----------

def _make_skill(name: str, triggers: list[str]) -> Skill:
    from manus.skills_loader import SkillMetadata
    return Skill(
        metadata=SkillMetadata(name=name, description="x", triggers=triggers),
        instructions="body",
    )


def test_detect_relevant_skills_matches_trigger():
    skills = {
        "research": _make_skill("research", ["исследование", "research"]),
        "browsing": _make_skill("browsing", ["browser", "web"]),
    }
    found = detect_relevant_skills("Сделай deep research по теме X", skills)
    assert "research" in found


def test_detect_relevant_skills_case_insensitive():
    skills = {"x": _make_skill("x", ["FooBar"])}
    found = detect_relevant_skills("here is foobar text", skills)
    assert "x" in found


def test_detect_relevant_skills_no_match():
    skills = {"x": _make_skill("x", ["nope"])}
    found = detect_relevant_skills("totally unrelated text", skills)
    assert found == []
