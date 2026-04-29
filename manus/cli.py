"""CLI: manus run / resume / status / list / kill / models."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .agent import Agent
from .config import CONFIG, MODELS, PATHS
from .workspace import Workspace, make_task_id

app = typer.Typer(add_completion=False, help="Manus Cloud — autonomous AI agent")
console = Console()


@app.command()
def run(
    task: str = typer.Argument(..., help="Описание задачи (в кавычках)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help=f"Модель: {list(MODELS)}"),
    task_id: Optional[str] = typer.Option(None, "--id", help="Свой task_id (default: автогенерация)"),
    max_iter: Optional[int] = typer.Option(None, "--max-iter", help="Лимит итераций"),
    summarizer: Optional[str] = typer.Option(None, "--summarizer", help="Модель для compaction"),
    groups: Optional[str] = typer.Option(
        None, "--groups", "-g",
        help="Активные группы тулов через запятую (file,shell,research,browser,memory,subagent). "
             "Default = все. lifecycle/communication всегда доступны.",
    ),
    traces: bool = typer.Option(
        False, "--traces", help="Включить Phoenix tracing (запустит локальный Phoenix UI)",
    ),
    phoenix_endpoint: Optional[str] = typer.Option(
        None, "--phoenix-endpoint",
        help="URL внешнего Phoenix collector (если не указан и --traces, запустится локальный)",
    ),
):
    """Запустить новую задачу."""
    PATHS.ensure()
    if traces or phoenix_endpoint:
        from .observability import setup_phoenix
        url = setup_phoenix(launch_local=not phoenix_endpoint, endpoint=phoenix_endpoint)
        if url:
            console.print(f"[bold magenta]Phoenix:[/] {url}")
    ws = Workspace.create(task, task_id=task_id)
    console.print(f"[bold green]Workspace:[/] {ws.root}")
    console.print(f"[bold green]Task ID:[/] {ws.task_id}")
    active_groups = [g.strip() for g in groups.split(",")] if groups else None
    agent = Agent(
        workspace=ws,
        executor_model=model,
        summarizer_model=summarizer,
        active_groups=active_groups,
    )
    console.print(f"[cyan]Executor:[/] {agent.executor.model.short} ({agent.executor.model.id})")
    console.print(f"[cyan]Summarizer:[/] {agent.summarizer.model.short}")
    console.print(f"[cyan]Tools:[/] {len(agent._all_specs)} total, "
                  f"{len(agent._active_tool_names())} active "
                  f"(groups: {active_groups or 'all'})")
    try:
        state = agent.run(max_iterations=max_iter)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. State saved.[/]")
        return
    _print_final(state, ws)


@app.command()
def resume(
    task_id: str = typer.Argument(..., help="task_id для возобновления"),
    max_iter: Optional[int] = typer.Option(None, "--max-iter"),
):
    """Возобновить ранее сохранённую сессию."""
    agent = Agent.resume(task_id)
    console.print(f"[bold green]Resumed:[/] {task_id} (iter={agent.state.iteration})")
    state = agent.run(max_iterations=max_iter)
    _print_final(state, agent.workspace)


@app.command()
def status(task_id: Optional[str] = typer.Argument(None)):
    """Показать статус задач(и)."""
    if task_id:
        ws = Workspace.load(task_id)
        st = ws.load_state()
        console.print(f"[bold]Task:[/] {task_id}")
        console.print(f"[bold]Phase:[/] {st.get('phase', 'unknown')}")
        console.print(f"[bold]Iter:[/] {st.get('iteration', 0)}")
        console.print(f"[bold]Done:[/] {st.get('done', False)}")
        console.print(f"[bold]Tokens:[/] {st.get('tokens', {})}")
        if ws.todo.exists():
            console.print(f"\n[bold]todo.md:[/]\n{ws.todo.read_text(encoding='utf-8')[:2000]}")
        return
    # list all
    PATHS.ensure()
    rows = []
    for d in sorted(PATHS.workspaces.iterdir()):
        if not d.is_dir():
            continue
        st = (d / "state.json")
        info = {"task_id": d.name, "phase": "?", "iter": 0, "done": False}
        if st.exists():
            try:
                data = json.loads(st.read_text(encoding="utf-8"))
                info["phase"] = data.get("phase", "?")
                info["iter"] = data.get("iteration", 0)
                info["done"] = data.get("done", False)
            except Exception:
                pass
        rows.append(info)
    table = Table(show_header=True)
    table.add_column("Task ID")
    table.add_column("Phase")
    table.add_column("Iter")
    table.add_column("Done")
    for r in rows:
        table.add_row(r["task_id"], r["phase"], str(r["iter"]), "✓" if r["done"] else "·")
    console.print(table)


@app.command()
def models():
    """Список доступных моделей."""
    table = Table(show_header=True)
    table.add_column("Short")
    table.add_column("ID")
    table.add_column("Context")
    table.add_column("Tool calling")
    table.add_column("Notes")
    for short, m in MODELS.items():
        table.add_row(short, m.id, f"{m.context_window:,}",
                      "✓" if m.supports_tool_calling else "✗", m.notes)
    console.print(table)


@app.command()
def tools():
    """Список инструментов агента."""
    from .tools import build_default_registry
    reg = build_default_registry()
    table = Table(show_header=True)
    table.add_column("Name")
    table.add_column("Group")
    table.add_column("Always")
    table.add_column("Side fx")
    table.add_column("Description")
    for name in sorted(reg.names()):
        t = reg.get(name)
        table.add_row(name, t.group,
                      "✓" if t.always_available else "—",
                      "yes" if t.side_effects else "—",
                      t.description[:60])
    console.print(table)


@app.command()
def groups():
    """Список групп тулов (для tool masking)."""
    from .tools import build_default_registry
    reg = build_default_registry()
    table = Table(show_header=True)
    table.add_column("Group")
    table.add_column("Tools")
    table.add_column("Always available")
    for grp, names in sorted(reg.groups().items()):
        always = [n for n in names if reg.get(n).always_available]
        table.add_row(grp, ", ".join(names),
                      ", ".join(always) if always else "—")
    console.print(table)
    console.print("\n[dim]Use `manus run --groups file,shell,memory` to restrict active set.[/]")
    console.print("[dim]Tools marked 'always_available' are never masked.[/]")


@app.command()
def pac1(
    benchmark: str = typer.Option("bitgn/pac1-dev", "--benchmark", "-b",
                                    help="bitgn/pac1-dev (43 tasks) или bitgn/pac1-prod (104)"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    summarizer: Optional[str] = typer.Option(None, "--summarizer"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Сколько задач максимум"),
    tasks: Optional[str] = typer.Option(None, "--tasks", "-t",
                                          help="Только эти task_id через запятую (t01,t02,...)"),
    no_submit: bool = typer.Option(False, "--no-submit", help="Не сабмитить run на сервер"),
    name: str = typer.Option("manus-agent-v1", "--name", help="Имя run'а в leaderboard"),
    max_iter: int = typer.Option(30, "--max-iter", help="Макс iterations per trial"),
):
    """Запустить наш agent на BitGN PAC1 benchmark."""
    from .pac1_runner import run_pac1
    task_filter = [t.strip() for t in tasks.split(",")] if tasks else None
    try:
        run_pac1(
            benchmark_id=benchmark,
            executor_model=model,
            summarizer_model=summarizer,
            limit=limit,
            task_filter=task_filter,
            submit=not no_submit,
            run_name=name,
            max_iter_per_trial=max_iter,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/]")


@app.command(name="check")
def check():
    """Sanity-check окружения (API key, deps, paths)."""
    import os
    from .config import get_model
    issues = 0
    console.print("[bold]Manus Cloud — environment check[/]\n")
    # API key
    for short in ("qwen-coder", "minimax", "glm"):
        m = get_model(short)
        key = os.environ.get(m.api_key_env, "")
        ok = bool(key)
        console.print(f"  {m.short}: API key in ${m.api_key_env}: {'✓' if ok else '✗ MISSING'}")
        if not ok:
            issues += 1
    # Paths
    PATHS.ensure()
    console.print(f"\n  workspaces: {PATHS.workspaces} {'✓' if PATHS.workspaces.exists() else '✗'}")
    # Tmux
    import subprocess
    tmux_ok = subprocess.run(["which", "tmux"], capture_output=True).returncode == 0
    console.print(f"  tmux: {'✓' if tmux_ok else '✗ install: brew install tmux'}")
    if not tmux_ok:
        issues += 1
    # Playwright
    try:
        import playwright  # noqa
        console.print("  playwright: ✓")
    except ImportError:
        console.print("  playwright: ✗ (optional, install via `pip install playwright && playwright install chromium`)")
    # TG
    if CONFIG.tg_enabled:
        console.print(f"  telegram: ✓ (user={CONFIG.tg_user_id})")
    else:
        console.print("  telegram: not configured (will use stdin/stdout fallback)")

    console.print(f"\n[bold]{'OK' if issues == 0 else f'{issues} issues'}[/]")


def _print_final(state, workspace: Workspace):
    console.print(f"\n[bold]Final state:[/] {state.phase.value}")
    console.print(f"[bold]Iterations:[/] {state.iteration}")
    console.print(f"[bold]Tokens:[/] prompt={state.total_prompt_tokens}, completion={state.total_completion_tokens}")
    if state.failure_reason:
        console.print(f"[red]Failure:[/] {state.failure_reason}")
    if state.final_summary:
        console.print(f"\n[bold]Summary:[/]\n{state.final_summary}")
    console.print(f"\n[bold]Workspace:[/] {workspace.root}")


if __name__ == "__main__":
    app()
