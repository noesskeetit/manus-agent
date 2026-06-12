from __future__ import annotations

import importlib


def test_workspace_create_respects_manus_workspace_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MANUS_WORKSPACE_ROOT", str(tmp_path / "workers"))

    import manus.config as config_module
    import manus.workspace as workspace_module

    importlib.reload(config_module)
    workspace_module = importlib.reload(workspace_module)

    workspace = workspace_module.Workspace.create("env workspace smoke", task_id="env-smoke")

    assert workspace.root == tmp_path / "workers" / "env-smoke"

    monkeypatch.delenv("MANUS_WORKSPACE_ROOT", raising=False)
    importlib.reload(config_module)
    importlib.reload(workspace_module)
