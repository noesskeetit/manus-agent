"""Knowledge module: автоопределение релевантных playbooks/knowledge файлов по тексту задачи.

По образцу Manus knowledge_module: подача релевантных best practices через event stream.
В нашем стеке — формируем `[Knowledge hints]` блок при старте задачи.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _index_path() -> Path:
    return Path(__file__).parent / "prompts" / "playbooks" / "_index.json"


def load_index() -> dict:
    p = _index_path()
    if not p.exists():
        return {"playbooks": [], "knowledge_files": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"playbooks": [], "knowledge_files": []}


def detect_relevant(task_text: str) -> dict:
    """По тексту задачи находим релевантные playbooks и knowledge.

    Returns: {"playbooks": [{file, title, why}], "knowledge_files": [...]}
    """
    text_lower = task_text.lower()
    idx = load_index()
    out = {"playbooks": [], "knowledge_files": []}
    for pb in idx.get("playbooks", []):
        for trigger in pb.get("triggers", []):
            if trigger.lower() in text_lower:
                out["playbooks"].append({
                    "file": pb["file"],
                    "title": pb.get("title", pb["file"]),
                    "matched_trigger": trigger,
                })
                break
    for kf in idx.get("knowledge_files", []):
        for trigger in kf.get("triggers", []):
            if trigger.lower() in text_lower:
                out["knowledge_files"].append({
                    "file": kf["file"],
                    "title": kf.get("title", kf["file"]),
                    "matched_trigger": trigger,
                })
                break
    return out


def render_hints(task_text: str) -> Optional[str]:
    """Сформировать секцию '[Knowledge hints]' для system prompt при старте.

    Возвращает None если ничего релевантного не найдено.
    """
    rel = detect_relevant(task_text)
    if not rel["playbooks"] and not rel["knowledge_files"]:
        return None
    lines = ["# === Knowledge hints (auto-detected) ===\n"]
    pb_root = Path(__file__).parent / "prompts" / "playbooks"
    if rel["playbooks"]:
        lines.append("Применимые playbooks (читай через `file_read` при необходимости):")
        for pb in rel["playbooks"]:
            full_path = (pb_root / pb["file"]).resolve()
            lines.append(f"- {full_path}\n  Why: {pb['title']} (матч: '{pb['matched_trigger']}')")
    if rel["knowledge_files"]:
        lines.append("\nДоменные знания:")
        for kf in rel["knowledge_files"]:
            full_path = (pb_root / kf["file"]).resolve()
            lines.append(f"- {full_path}\n  Why: {kf['title']} (матч: '{kf['matched_trigger']}')")
    return "\n".join(lines)
