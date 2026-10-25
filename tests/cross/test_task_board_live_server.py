"""Shared-board acceptance through real HTTP sockets and a process restart."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

from openhands.tools.task_board import TaskBoardAction
from openhands.tools.task_board.impl import TaskBoardExecutor
from openhands.tools.task_board.models import BoardTaskInput


@contextmanager
def task_board_server(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "conversations_path": str(tmp_path / "conversations"),
                "workspace_path": str(workspace),
                "bash_events_dir": str(tmp_path / "bash-events"),
                "session_api_keys": ["board-live-test"],
                "enable_browser": False,
                "enable_vscode": False,
                "preload_tools": False,
                "lease_ttl_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "OPENHANDS_AGENT_SERVER_CONFIG_PATH": str(config),
        "OH_PERSISTENCE_DIR": str(tmp_path / "settings"),
        "OH_SESSION_API_KEYS_0": "board-live-test",
        "PYTHONUTF8": "1",
    }
    log_path = tmp_path / f"server-{port}.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openhands.agent_server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=tmp_path,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=30,
                headers={"X-Session-API-Key": "board-live-test"},
            ) as client:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(log_path.read_text(encoding="utf-8"))
                    try:
                        if client.get("/ready", timeout=0.5).status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
                yield client
        finally:
            process.kill()
            process.wait(timeout=15)


def test_task_board_survives_real_server_restart_without_browser(tmp_path, monkeypatch):
    with task_board_server(tmp_path) as client:

        def start(parent=None):
            response = client.post(
                "/api/conversations",
                json={
                    "agent": {
                        "llm": {"model": "gpt-4o", "usage_id": "board-live"},
                        "tools": [],
                    },
                    "workspace": {"working_dir": str(tmp_path / "workspace")},
                    "parent_conversation_id": parent,
                },
            )
            assert response.status_code in (200, 201), response.text
            return response.json()["id"]

        root, outsider = start(), start()
        child = start(root)
        monkeypatch.setenv("OH_INTERNAL_SERVER_URL", str(client.base_url).rstrip("/"))
        monkeypatch.setenv("OH_SESSION_API_KEYS_0", "board-live-test")
        executor = TaskBoardExecutor(child)
        created = executor(
            TaskBoardAction(
                command="create", task=BoardTaskInput(title="live completion")
            )
        )
        assert not created.is_error and created.task is not None
        claimed = executor(
            TaskBoardAction(
                command="claim",
                task_id=created.task.id,
                expected_version=0,
                role="engineer",
            )
        )
        assert not claimed.is_error and claimed.task is not None
        contents = claimed.task.model_dump(exclude={"id", "version"})
        completed = executor(
            TaskBoardAction(
                command="update",
                task_id=claimed.task.id,
                expected_version=1,
                task=BoardTaskInput.model_validate(
                    {**contents, "status": "done", "completion_commit": "abcdef1234"}
                ),
            )
        )
        assert not completed.is_error
        active = executor(
            TaskBoardAction(command="create", task=BoardTaskInput(title="live claim"))
        )
        assert active.task is not None
        assert not executor(
            TaskBoardAction(
                command="claim",
                task_id=active.task.id,
                expected_version=0,
                role="engineer",
            )
        ).is_error
        expected = client.get(f"/api/conversations/{root}/task-board").json()
        assert len(expected["tasks"]) == 2
        rejected = client.get(
            f"/api/conversations/{outsider}/task-board",
            params={"root_conversation_id": root},
        )
        assert rejected.status_code == 403

    with task_board_server(tmp_path) as restarted:
        recovered = restarted.get(f"/api/conversations/{child}/task-board")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json() == expected
        assert restarted.delete(f"/api/conversations/{root}").status_code == 200
        assert (
            restarted.get(f"/api/conversations/{child}/task-board").status_code == 404
        )
    (tmp_path / "task-board-evidence.json").write_text(
        json.dumps(
            {
                "transport": "real HTTP; no browser or WebSocket; no model calls",
                "restart": "server process killed and restarted with same persistence",
                "before_restart": expected,
                "after_restart": recovered.json(),
                "out_of_tree_read": 403,
                "former_member_after_root_deletion": 404,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
