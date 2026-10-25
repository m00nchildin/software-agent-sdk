"""Transport failures and conflict state remain actionable to the calling agent."""

from uuid import uuid4

import httpx
import pytest

from openhands.tools.task_board import TaskBoardAction
from openhands.tools.task_board.impl import TaskBoardExecutor
from openhands.tools.task_board.models import BoardTask, BoardTaskInput


def test_tool_claim_binds_identity_and_returns_current_state(monkeypatch):
    member, task_id = uuid4(), uuid4()
    winner = BoardTask(
        id=task_id,
        title="work",
        version=1,
        owner=uuid4(),
        role="winner",
        status="in_progress",
    )
    monkeypatch.setenv("OH_INTERNAL_SERVER_URL", "http://board-server")
    monkeypatch.setenv("OH_SESSION_API_KEYS_0", "tool-session")

    def respond(request):
        assert (
            request.url.path
            == f"/api/conversations/{member}/task-board/tasks/{task_id}/claim"
        )
        assert request.headers["X-Session-API-Key"] == "tool-session"
        assert b'"expected_version":0' in request.content
        return httpx.Response(
            409,
            json={
                "detail": "Task version is stale",
                "current_task": winner.model_dump(mode="json"),
            },
        )

    executor = TaskBoardExecutor(member, httpx.MockTransport(respond))
    result = executor(
        TaskBoardAction(
            command="claim", task_id=task_id, expected_version=0, role="worker"
        )
    )
    assert result.is_error
    assert result.status_code == 409
    assert result.task == winner
    assert "tool-session" not in result.model_dump_json()


@pytest.mark.parametrize("failure", ["timeout", "invalid-response", "forbidden"])
def test_tool_failure_does_not_retry_mutations(failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "forbidden":
            return httpx.Response(403, json={"detail": "out of scope"})
        return httpx.Response(200, content="not json")

    executor = TaskBoardExecutor(uuid4(), httpx.MockTransport(respond))
    result = executor(
        TaskBoardAction(command="create", task=BoardTaskInput(title="work"))
    )
    assert result.is_error
    assert len(calls) == 1


def test_tool_validates_mutation_arguments_before_http():
    def unexpected(_request):
        pytest.fail("Invalid action must not reach the server")

    executor = TaskBoardExecutor(uuid4(), httpx.MockTransport(unexpected))
    for action in (
        TaskBoardAction(command="create"),
        TaskBoardAction(command="update"),
        TaskBoardAction(command="claim", task_id=uuid4(), expected_version=0),
    ):
        assert executor(action).is_error
