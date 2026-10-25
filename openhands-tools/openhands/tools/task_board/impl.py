"""HTTP executor bound to its conversation identity, independent of a browser."""

from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from openhands.sdk.tool import ToolExecutor
from openhands.tools.child_conversation.impl import (
    default_launch_url,
    session_api_key_headers,
)
from openhands.tools.task_board.definition import TaskBoardAction, TaskBoardObservation
from openhands.tools.task_board.models import BoardTask, TaskBoard, TaskBoardConflict


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation


class TaskBoardExecutor(ToolExecutor[TaskBoardAction, TaskBoardObservation]):
    def __init__(
        self,
        conversation_id: UUID,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.transport = transport

    def __call__(
        self,
        action: TaskBoardAction,
        conversation: "LocalConversation | None" = None,  # noqa: ARG002
    ) -> TaskBoardObservation:
        url = default_launch_url(self.conversation_id).removesuffix("/children")
        url += "/task-board"
        method = "GET"
        payload = None
        if action.command != "view":
            method = "POST"
            url += "/tasks"
            if action.command == "create":
                if action.task is None:
                    return self._error("create requires task contents")
                payload = action.task.model_dump(mode="json")
            else:
                if action.task_id is None or action.expected_version is None:
                    return self._error("update and claim require task_id and version")
                url += f"/{action.task_id}"
                if action.command == "claim":
                    if action.role is None:
                        return self._error("claim requires a role")
                    url += "/claim"
                    payload = {
                        "expected_version": action.expected_version,
                        "role": action.role,
                    }
                else:
                    if action.task is None:
                        return self._error("update requires complete task contents")
                    method = "PUT"
                    payload = {
                        **action.task.model_dump(mode="json"),
                        "expected_version": action.expected_version,
                    }
        params = (
            {"root_conversation_id": str(action.root_conversation_id)}
            if action.root_conversation_id
            else None
        )
        try:
            with httpx.Client(timeout=30, transport=self.transport) as client:
                response = client.request(
                    method,
                    url,
                    json=payload,
                    params=params,
                    headers=session_api_key_headers(),
                )
            if response.status_code == 409:
                conflict = TaskBoardConflict.model_validate(response.json())
                return TaskBoardObservation.from_text(
                    text=conflict.model_dump_json(),
                    task=conflict.current_task,
                    status_code=409,
                    is_error=True,
                )
            if response.is_error:
                return self._error(
                    f"Task board request rejected ({response.status_code})",
                    response.status_code,
                )
            if action.command == "view":
                board = TaskBoard.model_validate(response.json())
                return TaskBoardObservation.from_text(
                    text=board.model_dump_json(),
                    board=board,
                    status_code=response.status_code,
                )
            task = BoardTask.model_validate(response.json())
            return TaskBoardObservation.from_text(
                text=task.model_dump_json(), task=task, status_code=response.status_code
            )
        except httpx.TimeoutException:
            return self._error("Task board request timed out; view before retrying")
        except (httpx.HTTPError, ValueError):
            return self._error("Task board request failed; view before retrying")

    @staticmethod
    def _error(text: str, status_code: int | None = None) -> TaskBoardObservation:
        return TaskBoardObservation.from_text(
            text=text, is_error=True, status_code=status_code
        )
