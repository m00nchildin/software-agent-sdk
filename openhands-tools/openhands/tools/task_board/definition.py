"""Agent-facing tool for reading and updating a server-owned shared task board."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Literal, Self
from uuid import UUID

from pydantic import Field

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    register_tool,
)
from openhands.tools.task_board.models import BoardTask, BoardTaskInput, TaskBoard


if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState


class TaskBoardAction(Action):
    """Read, create, replace, or atomically claim a team task."""

    command: Literal["view", "create", "update", "claim"] = "view"
    task_id: UUID | None = None
    task: BoardTaskInput | None = None
    expected_version: int | None = Field(default=None, ge=0)
    role: str | None = Field(default=None, min_length=1, max_length=100)
    root_conversation_id: UUID | None = None


class TaskBoardObservation(Observation):
    """The board, changed task, or current task returned by a conflict."""

    board: TaskBoard | None = None
    task: BoardTask | None = None
    status_code: int | None = None


class TaskBoardTool(ToolDefinition[TaskBoardAction, TaskBoardObservation]):
    """Coordinate with members of this conversation's tree through the server."""

    catalog_description: ClassVar[str] = "Read and claim tasks shared by an agent team."

    @classmethod
    def create(cls, conv_state: "ConversationState") -> Sequence[Self]:
        from openhands.tools.task_board.impl import TaskBoardExecutor

        return [
            cls(
                description=(
                    "Read and update this team's server-owned task board. Use view "
                    "before changing a task. create takes task contents; update "
                    "replaces all task contents and needs task_id, task, and the "
                    "expected_version you read. claim takes task_id, role, and "
                    "expected_version, atomically setting you as owner only when "
                    "the task is unowned, todo, and every dependency is done. "
                    "On a conflict, read the returned current task before deciding "
                    "what to do. To complete, update status to done and include the "
                    "completion_commit. blocked requires {reason, by}; by and owner "
                    "are conversation ids in this tree, and role is only a label. "
                    "Release with owner=null, role=null, status=todo. Do not put "
                    "credentials in the board. A timeout may have committed a "
                    "write: view the board before retrying. Requires Agent Server."
                ),
                action_type=TaskBoardAction,
                observation_type=TaskBoardObservation,
                executor=TaskBoardExecutor(conv_state.id),
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
            )
        ]


register_tool(TaskBoardTool.name, TaskBoardTool)
