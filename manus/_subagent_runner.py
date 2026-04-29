"""Sub-agent runner — запускается как subprocess.

Читает sub_root/input.json, гоняет агента в изоляции, пишет sub_root/output.json.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: _subagent_runner.py <sub_root>", file=sys.stderr)
        return 2
    sub_root = Path(sys.argv[1])
    inp_path = sub_root / "input.json"
    out_path = sub_root / "output.json"
    if not inp_path.exists():
        print(f"ERROR: {inp_path} not found", file=sys.stderr)
        return 2

    inp = json.loads(inp_path.read_text(encoding="utf-8"))
    task = inp.get("task", "")
    scope = inp.get("scope", {}) or {}
    model = inp.get("model")
    max_iter = inp.get("max_iterations", 40)
    allowed_tools = inp.get("allowed_tools")
    role = inp.get("role")
    active_groups = inp.get("active_groups")
    recursion_depth = inp.get("recursion_depth", 1)
    # Set env для дальнейших spawn'ов (recursion guard)
    import os as _os
    _os.environ["MANUS_SUBAGENT_RECURSION_DEPTH"] = str(recursion_depth)

    # Импортируем здесь чтобы можно было запускать как стандалон
    from manus.workspace import Workspace
    from manus.tools import build_default_registry
    from manus.agent import Agent

    # У sub-agent'а свой workspace — sub_root
    ws = Workspace(task_id=inp.get("sub_id", "sub"), root=sub_root, task_text=task)
    if not ws.todo.exists():
        ws.todo.write_text(
            f"# Sub-task\n\n{task}\n\n## Scope\n\n"
            f"in_scope: {scope.get('in_scope', [])}\n"
            f"out_of_scope: {scope.get('out_of_scope', [])}\n"
            f"deliverables: {scope.get('deliverables', [])}\n\n"
            "## План\n\n"
            "## Текущее состояние\n\n## Заметки\n",
            encoding="utf-8",
        )
    if not ws.journal.exists():
        ws.journal.write_text("# Sub-agent journal\n\n", encoding="utf-8")

    # Подбираем тулы. Гарантируем что always_available тулы (idle, notify) всегда есть,
    # иначе sub-agent не сможет завершиться.
    registry = build_default_registry()
    if allowed_tools:
        allowed = set(allowed_tools)
        # forced add always_available tools
        for n, t in list(registry._tools.items()):
            if t.always_available:
                allowed.add(n)
        registry._tools = {n: t for n, t in registry._tools.items() if n in allowed}

    # Адаптируем system prompt: read-only режим + role-specific
    role_prompt = ""
    if role:
        role_path = Path(__file__).parent / "prompts" / "roles" / f"{role}.md"
        if role_path.exists():
            role_prompt = "\n\n# === ROLE ===\n\n" + role_path.read_text(encoding="utf-8")
        else:
            role_prompt = f"\n\n# Role: {role} (prompt file not found, using generic)"

    system_extra = (
        "\n\n## SUB-AGENT MODE\n\n"
        f"Ты — sub-agent (role={role or 'generic'}, depth={recursion_depth}). "
        "Работаешь в изоляции в своём workspace.\n"
        f"In scope: {scope.get('in_scope', 'as in task')}\n"
        f"Out of scope: {scope.get('out_of_scope', 'modifying parent files')}\n"
        f"Deliverables: {scope.get('deliverables', 'see task description')}\n\n"
        "Запрещено:\n"
        "- message_ask_user (parent agent сам общается с пользователем)\n"
        "- Запись за пределы своего workspace\n"
        "- Действия с побочкой (publish, deploy, send) если это явно не in_scope\n\n"
        "По завершении — idle с кратким summary 200-400 слов."
    )
    base_prompt = Agent._load_default_system_prompt()
    full_prompt = base_prompt + role_prompt + system_extra

    agent = Agent(
        workspace=ws,
        registry=registry,
        executor_model=model,
        system_prompt=full_prompt,
        active_groups=active_groups,
    )

    try:
        result_state = agent.run(max_iterations=max_iter)
    except Exception as e:
        traceback.print_exc()
        out_path.write_text(json.dumps({
            "status": "failed",
            "summary": f"Sub-agent crashed: {type(e).__name__}: {e}",
            "error": str(e),
            "artifacts": [],
            "findings": {},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    summary = result_state.final_summary
    if not summary and ws.summary.exists():
        summary = ws.summary.read_text(encoding="utf-8")[:3000]
    if not summary:
        summary = "(no summary produced)"

    artifacts = []
    for d in (ws.artifacts_dir, ws.research_dir):
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file():
                    artifacts.append(str(p))

    out = {
        "status": "completed" if result_state.done else "failed",
        "summary": summary,
        "artifacts": artifacts,
        "findings": {
            "iterations": result_state.iteration,
            "tokens_prompt": result_state.total_prompt_tokens,
            "tokens_completion": result_state.total_completion_tokens,
            "phase": result_state.phase.value,
            "failure_reason": result_state.failure_reason,
        },
        "error": result_state.failure_reason or None,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result_state.done else 1


if __name__ == "__main__":
    sys.exit(main())
