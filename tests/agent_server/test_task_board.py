"""Shared-board acceptance tests using real conversations and persisted state."""

import asyncio
import multiprocessing
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from openhands.agent_server.api import create_app
from openhands.agent_server.config import Config
from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.models import StartConversationRequest
from openhands.agent_server.task_board import TaskBoardStore, TaskConflict
from openhands.sdk import LLM, Agent
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.task_board.models import BoardTaskInput, ClaimBoardTaskRequest


@pytest.fixture
async def team(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = tmp_path / "conversations"
    async with ConversationService(
        conversations_dir=directory, enable_browser=False
    ) as service:

        async def start(parent=None):
            info, _ = await service.start_conversation(
                StartConversationRequest(
                    agent=Agent(
                        llm=LLM(model="gpt-4o", usage_id="board-tests"), tools=[]
                    ),
                    workspace=LocalWorkspace(working_dir=str(workspace)),
                    confirmation_policy=NeverConfirm(),
                    parent_conversation_id=parent,
                )
            )
            return info.id

        root = await start()
        first = await start(root)
        second = await start(root)
        grandchild = await start(first)
        outsider = await start()
        app = create_app(Config(session_api_keys=["board-tests"]))
        app.state.conversation_service = service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://board-tests",
            headers={"X-Session-API-Key": "board-tests"},
        ) as client:
            yield client, service, (root, first, second, grandchild, outsider)


def board_url(member, suffix=""):
    return f"/api/conversations/{member}/task-board{suffix}"


async def create_task(client, member, **contents):
    response = await client.post(
        board_url(member, "/tasks"), json={"title": "shared work", **contents}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def replace_task(client, member, task, **updates):
    contents = {
        key: value for key, value in task.items() if key not in ("id", "version")
    }
    return await client.put(
        board_url(member, f"/tasks/{task['id']}"),
        json={**contents, **updates, "expected_version": task["version"]},
    )


async def test_atomic_claim_and_duplicate_rejection(team):
    client, _, (root, first, second, grandchild, _) = team
    task = await create_task(client, root)
    responses = await asyncio.gather(
        *(
            client.post(
                board_url(member, f"/tasks/{task['id']}/claim"),
                json={"expected_version": 0, "role": "engineer"},
            )
            for member in (first, second)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(
        response.json() for response in responses if response.status_code == 200
    )
    loser = next(
        response.json() for response in responses if response.status_code == 409
    )
    assert loser["current_task"] == winner
    assert winner["owner"] in (str(first), str(second))
    duplicate = await client.post(
        board_url(winner["owner"], f"/tasks/{task['id']}/claim"),
        json={"expected_version": winner["version"], "role": "engineer"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["current_task"] == winner
    for member in (root, first, second, grandchild):
        board = await client.get(board_url(member))
        assert board.status_code == 200
        assert board.json()["tasks"] == [winner]


async def test_dependencies_completion_and_blocked_lifecycle(team):
    client, _, (root, first, second, _, _) = team
    dependency = await create_task(client, root)
    task = await create_task(client, root, depends_on=[dependency["id"]])
    url = board_url(first, f"/tasks/{task['id']}/claim")
    claim = {"expected_version": 0, "role": "engineer"}
    refused = await client.post(url, json=claim)
    assert refused.status_code == 409
    assert refused.json()["current_task"] == task
    dep_claim = await client.post(
        board_url(second, f"/tasks/{dependency['id']}/claim"), json=claim
    )
    dependency = dep_claim.json()
    missing_commit = await replace_task(client, second, dependency, status="done")
    assert missing_commit.status_code == 422
    completed = await replace_task(
        client, second, dependency, status="completed", completion_commit="abcdef1234"
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    assert completed.json()["completion_commit"] == "abcdef1234"
    acquired = await client.post(url, json=claim)
    assert acquired.status_code == 200
    blocked = await replace_task(
        client,
        first,
        acquired.json(),
        status="blocked",
        blocked={"reason": "waiting for review", "by": str(second)},
    )
    assert blocked.status_code == 200
    unblocked = await replace_task(client, first, blocked.json(), status="in_progress")
    assert unblocked.status_code == 200
    assert unblocked.json()["blocked"] is None
    stale = await replace_task(client, first, blocked.json(), status="todo")
    assert stale.status_code == 409
    assert stale.json()["current_task"] == unblocked.json()


async def test_tree_scope_and_auth_reject_without_mutation(team):
    client, _, (root, first, _, _, outsider) = team
    task = await create_task(client, first)
    before = (await client.get(board_url(root))).json()
    params = {"root_conversation_id": str(root)}
    for method, suffix, payload in (
        ("GET", "", None),
        ("POST", "/tasks", {"title": "intrusion"}),
        (
            "POST",
            f"/tasks/{task['id']}/claim",
            {"expected_version": 0, "role": "intruder"},
        ),
    ):
        response = await client.request(
            method, board_url(outsider, suffix), params=params, json=payload
        )
        assert response.status_code == 403
    foreign_owner = await client.post(
        board_url(root, "/tasks"),
        json={"title": "intrusion", "owner": str(outsider), "role": "intruder"},
    )
    assert foreign_owner.status_code == 403
    unknown = await client.get(board_url(uuid4()))
    assert unknown.status_code == 404
    unauthenticated = await client.get(
        board_url(root), headers={"X-Session-API-Key": "wrong"}
    )
    assert unauthenticated.status_code == 401
    assert (await client.get(board_url(root))).json() == before


@pytest.mark.parametrize(
    "contents",
    [
        {"role": "engineer"},
        {"status": "doing"},
        {"status": "blocked"},
        {"depends_on": [str(uuid4())]},
        {"api_key": "must-not-persist"},
    ],
)
async def test_invalid_task_stores_nothing(team, contents):
    client, service, (root, _, _, _, _) = team
    response = await client.post(
        board_url(root, "/tasks"), json={"title": "invalid", **contents}
    )
    assert response.status_code == 422
    assert "must-not-persist" not in response.text
    assert (await client.get(board_url(root))).json()["tasks"] == []
    assert not (service.conversations_dir / root.hex / "task_board.json").exists()


async def test_restart_preserves_claim_and_completion_and_root_owns_teardown(team):
    client, service, (root, first, second, _, _) = team
    task = await create_task(client, first)
    claimed = await client.post(
        board_url(first, f"/tasks/{task['id']}/claim"),
        json={"expected_version": 0, "role": "engineer"},
    )
    recovered_store = TaskBoardStore(service.conversations_dir)
    assert (
        recovered_store.read(second).tasks[0].model_dump(mode="json") == claimed.json()
    )
    completed = await replace_task(
        client, first, claimed.json(), status="done", completion_commit="abcdef1234"
    )
    expected = (await client.get(board_url(root))).json()
    assert completed.status_code == 200
    async with ConversationService(
        conversations_dir=service.conversations_dir, enable_browser=False
    ) as restarted:
        recovered = TaskBoardStore(restarted.conversations_dir).read(second)
        assert recovered.model_dump(mode="json") == expected
    assert await service.delete_conversation(first)
    assert (await client.get(board_url(second))).json() == expected
    assert await service.delete_conversation(root)
    assert not (service.conversations_dir / root.hex / "task_board.json").exists()
    assert (await client.get(board_url(second))).status_code == 404
    refused = await client.post(board_url(second, "/tasks"), json={"title": "late"})
    assert refused.status_code == 404


async def test_dependency_cycle_and_owner_transfer_are_rejected(team):
    client, _, (root, first, second, _, _) = team
    first_task = await create_task(client, root)
    second_task = await create_task(client, root, depends_on=[first_task["id"]])
    cycle = await replace_task(client, root, first_task, depends_on=[second_task["id"]])
    assert cycle.status_code == 422
    claimed = await client.post(
        board_url(first, f"/tasks/{first_task['id']}/claim"),
        json={"expected_version": 0, "role": "engineer"},
    )
    transfer = await replace_task(client, second, claimed.json(), owner=str(second))
    assert transfer.status_code == 409
    assert transfer.json()["current_task"] == claimed.json()
    released = await replace_task(
        client, first, claimed.json(), owner=None, role=None, status="pending"
    )
    assert released.status_code == 200
    assert released.json()["status"] == "todo"
    reclaimed = await client.post(
        board_url(second, f"/tasks/{first_task['id']}/claim"),
        json={"expected_version": released.json()["version"], "role": "engineer"},
    )
    assert reclaimed.status_code == 200
    assert reclaimed.json()["owner"] == str(second)


def _process_claim(directory, member, task_id, barrier):
    store = TaskBoardStore(Path(directory))
    barrier.wait(timeout=30)
    try:
        task = store.claim(
            UUID(member),
            UUID(task_id),
            ClaimBoardTaskRequest(expected_version=0, role="worker"),
        )
        return 200, task.model_dump(mode="json")
    except TaskConflict as exc:
        return 409, exc.current_task.model_dump(mode="json")


async def test_claim_is_atomic_across_processes(team):
    _, service, (root, first, second, _, _) = team
    store = TaskBoardStore(service.conversations_dir)
    task = store.create(root, BoardTaskInput(title="cross-process race"))
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(2)
        with context.Pool(2) as pool:
            results = pool.starmap(
                _process_claim,
                [
                    (str(service.conversations_dir), str(member), str(task.id), barrier)
                    for member in (first, second)
                ],
            )
    assert sorted(status for status, _ in results) == [200, 409]
    assert results[0][1] == results[1][1]
    assert store.read(root).tasks[0].model_dump(mode="json") == results[0][1]


async def test_corrupt_board_is_not_overwritten(team):
    client, service, (root, _, _, _, _) = team
    await create_task(client, root)
    path = service.conversations_dir / root.hex / "task_board.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        TaskBoardStore(service.conversations_dir).create(
            root, BoardTaskInput(title="lost")
        )
    assert path.read_text(encoding="utf-8") == "{broken"


async def test_failed_write_keeps_previous_claim(team, monkeypatch):
    _, service, (root, first, _, _, _) = team
    store = TaskBoardStore(service.conversations_dir)
    task = store.create(root, BoardTaskInput(title="preserve me"))

    def fail_write(_path, _value):
        raise OSError("disk write failed")

    monkeypatch.setattr(
        "openhands.agent_server.task_board.atomic_write_text", fail_write
    )
    with pytest.raises(OSError, match="disk write failed"):
        store.claim(
            first, task.id, ClaimBoardTaskRequest(expected_version=0, role="worker")
        )
    assert store.read(root).tasks == [task]


async def test_root_deletion_racing_claim_cannot_resurrect_board(team):
    client, service, (root, first, _, _, _) = team
    task = await create_task(client, root)
    deleted, claimed = await asyncio.gather(
        client.delete(f"/api/conversations/{root}"),
        client.post(
            board_url(first, f"/tasks/{task['id']}/claim"),
            json={"expected_version": 0, "role": "worker"},
        ),
    )
    assert deleted.status_code == 200
    assert claimed.status_code in (200, 404)
    assert (await client.get(board_url(first))).status_code == 404
    assert not (service.conversations_dir / root.hex).exists()
