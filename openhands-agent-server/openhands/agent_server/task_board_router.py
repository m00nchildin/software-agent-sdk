"""Canonical REST operations for a conversation tree's shared task board."""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.dependencies import get_conversation_service
from openhands.agent_server.task_board import (
    TaskBoardError,
    TaskBoardStore,
    TaskConflict,
)
from openhands.tools.task_board.models import (
    BoardTask,
    BoardTaskInput,
    ClaimBoardTaskRequest,
    TaskBoard,
    TaskBoardConflict,
    UpdateBoardTaskRequest,
)


class TaskBoardRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Rejected extra fields may themselves contain credentials.
                errors = [
                    {key: error[key] for key in ("loc", "msg", "type")}
                    for error in exc.errors()
                ]
                raise HTTPException(422, errors) from exc

        return validated


task_board_router = APIRouter(
    prefix="/conversations/{conversation_id}/task-board",
    tags=["Task board"],
    route_class=TaskBoardRoute,
)
Root = Annotated[
    UUID | None,
    Query(description="Expected tree root; a different tree is rejected with 403."),
]


def get_task_board_store(
    service: ConversationService = Depends(get_conversation_service),
) -> TaskBoardStore:
    return TaskBoardStore(service.conversations_dir)


async def _operate[T](operation: Callable[[], T]) -> T | JSONResponse:
    try:
        return await asyncio.to_thread(operation)
    except TaskConflict as exc:
        return JSONResponse(
            status_code=409,
            content=TaskBoardConflict(
                detail=exc.detail, current_task=exc.current_task
            ).model_dump(mode="json"),
        )
    except TaskBoardError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@task_board_router.get("", response_model=TaskBoard)
async def read_task_board(
    conversation_id: UUID,
    root_conversation_id: Root = None,
    store: TaskBoardStore = Depends(get_task_board_store),
) -> TaskBoard | JSONResponse:
    """Read the whole team's board without starting a runtime or attaching a client.

    The authenticated server caller supplies a member conversation id. The server
    resolves its root from persisted parent links; an explicitly requested root
    must match that tree. The session key retains its existing server-wide scope.
    """
    return await _operate(partial(store.read, conversation_id, root_conversation_id))


@task_board_router.post("/tasks", response_model=BoardTask, status_code=201)
async def create_board_task(
    conversation_id: UUID,
    request: BoardTaskInput,
    root_conversation_id: Root = None,
    store: TaskBoardStore = Depends(get_task_board_store),
) -> BoardTask | JSONResponse:
    """Create a task with a server-assigned stable id and an initial revision.

    Owners and blockers must belong to this tree. A role alone is not an identity.
    Unknown or cyclic dependencies are rejected without changing the board.
    """
    return await _operate(
        partial(store.create, conversation_id, request, root_conversation_id)
    )


@task_board_router.put(
    "/tasks/{task_id}",
    response_model=BoardTask,
    responses={409: {"model": TaskBoardConflict}},
)
async def update_board_task(
    conversation_id: UUID,
    task_id: UUID,
    request: UpdateBoardTaskRequest,
    root_conversation_id: Root = None,
    store: TaskBoardStore = Depends(get_task_board_store),
) -> BoardTask | JSONResponse:
    """Replace a task at its observed revision, or return 409 with current state.

    Acquire ownership through claim. Release it with owner=null and status=todo.
    Completing requires a commit; leaving blocked clears the blocking payload.
    """
    return await _operate(
        partial(store.update, conversation_id, task_id, request, root_conversation_id)
    )


@task_board_router.post(
    "/tasks/{task_id}/claim",
    response_model=BoardTask,
    responses={409: {"model": TaskBoardConflict}},
)
async def claim_board_task(
    conversation_id: UUID,
    task_id: UUID,
    request: ClaimBoardTaskRequest,
    root_conversation_id: Root = None,
    store: TaskBoardStore = Depends(get_task_board_store),
) -> BoardTask | JSONResponse:
    """Atomically claim an unowned todo task for this member at expected_version.

    Every dependency must be done. A stale revision or a competing claim returns
    409 and the current task, including its actual owner and revision.
    """
    return await _operate(
        partial(store.claim, conversation_id, task_id, request, root_conversation_id)
    )
