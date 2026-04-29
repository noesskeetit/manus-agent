"""Workspace per task — todo.md, journal.md, observations dump, summary.md.

По принципам Manus: filesystem = memory. Большие observations всегда на диск.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import PATHS


# ---------- Secret masking ----------

# Известные паттерны секретов. Применяется к session.jsonl превью и observations.
# Стараемся минимизировать false positives на ID/timestamps/UUID:
#   - TG bot: требуем строку начинаться с api.telegram.org/bot ИЛИ короткий префикс bot
#   - sk-: требуем минимум 40 chars или префикс sk-proj-/sk-ant-
#   - GitHub PAT: длина 36 — зафиксирована форматом
_SECRET_PATTERNS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY_REDACTED]"),
    # OpenAI sk-proj- / sk-ant- (явные префиксы) или sk-XXX длиной >=40
    (re.compile(r"\bsk-(proj|ant|svcacct)-[A-Za-z0-9_-]{20,}\b"), "[OPENAI_KEY_REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{40,}\b"), "[OPENAI_KEY_REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[SLACK_TOKEN_REDACTED]"),
    # TG bot token — только в составе URL (bot<digits>:<token>) или после "bot=" / "token="
    (re.compile(r"(?:api\.telegram\.org/bot|bot=|token=)[0-9]{8,12}:[A-Za-z0-9_-]{30,}",
                re.IGNORECASE), "[TG_BOT_TOKEN_REDACTED]"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"),
     "[PRIVATE_KEY_REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "[GH_TOKEN_REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "[GH_PAT_REDACTED]"),
]


def mask_secrets(text: str) -> str:
    """Применить regex-маскинг известных паттернов секретов.

    Можно отключить через env `MANUS_DISABLE_MASKING=true` для отладки.
    """
    if not text:
        return text
    import os as _os
    if _os.environ.get("MANUS_DISABLE_MASKING", "").lower() in ("1", "true", "yes"):
        return text
    out = text
    for rgx, repl in _SECRET_PATTERNS:
        out = rgx.sub(repl, out)
    return out


def _slugify(s: str, max_len: int = 60) -> str:
    """Превращает произвольный текст в URL/file-safe slug. Кириллица → транслит."""
    translit_map = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    s = s.lower()
    out = []
    for ch in s:
        if ch in translit_map:
            out.append(translit_map[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("-")
    slug = "".join(out)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len] or "task"


def make_task_id(task_text: str) -> str:
    """task_id = YYYY-MM-DD-<slug>-<short-hash>."""
    slug_words = task_text.split()[:6]
    slug = _slugify("-".join(slug_words))
    short_hash = hashlib.sha1(
        f"{task_text}-{time.time()}".encode()
    ).hexdigest()[:6]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{today}-{slug}-{short_hash}"


# ---------- Workspace ----------

@dataclass
class Workspace:
    """Per-task workspace на диске. Все артефакты задачи."""
    task_id: str
    root: Path
    task_text: str = ""
    _event_counter: int = 0   # in-memory cache для events/NNNN.json — избегаем O(n²) glob

    @property
    def todo(self) -> Path:
        return self.root / "todo.md"

    @property
    def journal(self) -> Path:
        return self.root / "journal.md"

    @property
    def summary(self) -> Path:
        return self.root / "summary.md"

    @property
    def session_log(self) -> Path:
        return self.root / "session.jsonl"

    @property
    def events_dir(self) -> Path:
        d = self.root / "events"
        d.mkdir(exist_ok=True)
        return d

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def observations_dir(self) -> Path:
        d = self.root / "observations"
        d.mkdir(exist_ok=True)
        return d

    @property
    def artifacts_dir(self) -> Path:
        d = self.root / "artifacts"
        d.mkdir(exist_ok=True)
        return d

    @property
    def research_dir(self) -> Path:
        d = self.root / "research"
        d.mkdir(exist_ok=True)
        return d

    @classmethod
    def create(cls, task_text: str, task_id: Optional[str] = None) -> "Workspace":
        PATHS.ensure()
        task_id = task_id or make_task_id(task_text)
        root = PATHS.workspaces / task_id
        root.mkdir(parents=True, exist_ok=True)
        ws = cls(task_id=task_id, root=root, task_text=task_text)
        # Stub файлы
        if not ws.todo.exists():
            ws.todo.write_text(
                f"# Задача\n\n{task_text}\n\n"
                f"task_id: {task_id}\n"
                f"started_at: {datetime.now(timezone.utc).isoformat()}\n\n"
                "## План\n\n_Декомпозируй задачу здесь._\n\n"
                "## Текущее состояние\n\n_Начало._\n\n"
                "## Заметки\n\n",
                encoding="utf-8",
            )
        if not ws.journal.exists():
            ws.journal.write_text(
                f"# Journal — {task_id}\n\n"
                f"Задача: {task_text}\n\n"
                f"Старт: {datetime.now(timezone.utc).isoformat()}\n\n",
                encoding="utf-8",
            )
        return ws

    @classmethod
    def load(cls, task_id: str) -> "Workspace":
        root = PATHS.workspaces / task_id
        if not root.exists():
            raise FileNotFoundError(f"Workspace {root} not found")
        # Пытаемся достать task_text из todo.md
        task_text = ""
        if (root / "todo.md").exists():
            txt = (root / "todo.md").read_text(encoding="utf-8")
            m = re.search(r"# Задача\n\n(.+?)\n", txt, re.DOTALL)
            if m:
                task_text = m.group(1).strip()
        return cls(task_id=task_id, root=root, task_text=task_text)

    def append_journal(self, entry: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(f"\n## {ts}\n\n{entry.strip()}\n")

    # ---------- Observation dump ----------

    def dump_observation(self, name: str, content: str,
                         turn_id: Optional[int] = None,
                         compress: bool = False) -> Path:
        """Дампит большой observation на диск. Возвращает путь.

        Auto-gzip для обзёрвейшнов >50KB. Применяет secret masking.
        """
        slug = _slugify(name, max_len=40)
        suffix = f"-{turn_id:04d}" if turn_id is not None else ""
        masked = mask_secrets(content)
        # Auto-compress если большой
        if not compress and len(masked) > 50_000:
            compress = True
        ext = ".txt.gz" if compress else ".txt"
        path = self.observations_dir / f"obs{suffix}-{slug}{ext}"
        try:
            if compress:
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    f.write(masked)
            else:
                path.write_text(masked, encoding="utf-8")
        except OSError as e:
            # На out-of-disk fallback к stub-файлу
            try:
                path.write_text(f"[dump failed: {e}]\n", encoding="utf-8")
            except OSError:
                pass
        return path

    def read_observation(self, path: str | Path,
                          start_line: int = 0,
                          end_line: int | None = None) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = self.observations_dir / p.name
        if str(p).endswith(".gz"):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                txt = f.read()
        else:
            txt = p.read_text(encoding="utf-8")
        if start_line == 0 and end_line is None:
            return txt
        lines = txt.splitlines()
        return "\n".join(lines[start_line:end_line])

    def grep_observations(self, pattern: str, max_hits: int = 50,
                           per_file_byte_limit: int = 5_000_000) -> list[dict]:
        """Простой grep по всем observations (для recall тула).

        Читает файлы по одному с лимитом байт на файл, чтобы не словить OOM
        на разрастающемся observations/.
        """
        try:
            rgx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Если паттерн не валидный regex — fall back на substring search
            rgx = re.compile(re.escape(pattern), re.IGNORECASE)
        hits: list[dict] = []
        for p in sorted(self.observations_dir.glob("*.txt*")):
            try:
                # Не читаем целиком если файл огромен
                if p.stat().st_size > per_file_byte_limit:
                    txt = self.read_observation(p, end_line=10_000)  # head only
                else:
                    txt = self.read_observation(p)
            except Exception:
                continue
            for i, line in enumerate(txt.splitlines()):
                if rgx.search(line):
                    hits.append({"path": str(p.relative_to(self.root)),
                                 "line_no": i, "line": line.strip()[:300]})
                    if len(hits) >= max_hits:
                        return hits
        return hits

    # ---------- Session log (JSONL) + Append-only EventLog (per-event files) ----------

    def append_session(self, entry: dict) -> None:
        entry = {**entry, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        # Маскируем секреты в превью (content_preview, args_preview, output, error)
        for key in ("content_preview", "output", "error", "args_preview"):
            if key in entry and isinstance(entry[key], str):
                entry[key] = mask_secrets(entry[key])
        if "tool_calls" in entry and isinstance(entry["tool_calls"], list):
            for tc in entry["tool_calls"]:
                if isinstance(tc, dict) and "args_preview" in tc:
                    tc["args_preview"] = mask_secrets(tc["args_preview"])
        # 1. Plain JSONL — удобно для grep
        with self.session_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # 2. Append-only EventLog (OpenHands pattern) — N-й файл в events/
        try:
            # Lazy init counter on first call: дорого один раз, потом O(1)
            if self._event_counter == 0:
                existing = sum(1 for _ in self.events_dir.glob("*.json"))
                self._event_counter = existing
            self._event_counter += 1
            event_id = self._event_counter
            ev_path = self.events_dir / f"{event_id:06d}.json"
            tmp = ev_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"event_id": event_id, **entry},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(ev_path)
        except Exception:
            # event log — best-effort, не валим основной поток
            pass

    def read_session(self, last_n: Optional[int] = None) -> list[dict]:
        if not self.session_log.exists():
            return []
        lines = self.session_log.read_text(encoding="utf-8").splitlines()
        if last_n is not None:
            lines = lines[-last_n:]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    # ---------- State (atomic rename) ----------

    def save_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
            f.flush()
            try:
                import os as _os
                _os.fsync(f.fileno())
            except OSError:
                pass
        tmp.replace(self.state_file)

    def load_state(self) -> dict:
        if not self.state_file.exists():
            return {}
        # Cleanup leftover tmp file (от прерванной save_state)
        tmp = self.state_file.with_suffix(".json.tmp")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger = __import__("logging").getLogger("manus.workspace")
            logger.warning("state.json corrupted (%s) — starting from blank state", e)
            return {}
