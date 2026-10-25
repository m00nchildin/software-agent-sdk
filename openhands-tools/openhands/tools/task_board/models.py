"""Wire models for the server-owned, conversation-tree task board."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openhands.tools.task_tracker.definition import TaskItem


class BoardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskBlocker(BoardModel):
    """Why a task is blocked and the conversation responsible for unblocking it."""

    reason: str = Field(min_length=1, max_length=4000)
    by: UUID


class BoardTaskInput(BoardModel):
    """Complete task contents; ownership is acquired through the claim operation."""

    title: str = Field(min_length=1, max_length=1000)
    owner: UUID | None = None
    role: str | None = Field(default=None, min_length=1, max_length=100)
    status: Literal["todo", "in_progress", "done", "blocked"] = "todo"
    depends_on: list[UUID] = Field(default_factory=list, max_length=1000)
    parallel_group: str | None = Field(default=None, max_length=100)
    blocked: TaskBlocker | None = None
    completion_commit: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,64}$")

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() == "blocked":
            return "blocked"
        return TaskItem.model_validate({"title": "", "status": value}).status

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.role is not None and self.owner is None:
            raise ValueError("A role requires an owner conversation id")
        if self.status == "blocked":
            if self.blocked is None:
                raise ValueError("A blocked task requires reason and by")
        else:
            self.blocked = None
        if self.status in ("in_progress", "done") and self.owner is None:
            raise ValueError("An active or completed task requires an owner")
        if self.status == "done":
            if self.completion_commit is None:
                raise ValueError("A completed task requires its completion commit")
        else:
            self.completion_commit = None
        return self


class BoardTask(BoardTaskInput):
    """A stable task identity and the revision required by its next mutation."""

    id: UUID
    version: int = Field(ge=0)


class TaskBoard(BoardModel):
    """The board owned by a tree's root conversation."""

    root_conversation_id: UUID
    tasks: list[BoardTask] = Field(default_factory=list)


class UpdateBoardTaskRequest(BoardTaskInput):
    """Replace task contents only if the supplied revision is current."""

    expected_version: int = Field(ge=0)


class ClaimBoardTaskRequest(BoardModel):
    """Claim for the requesting conversation at a previously observed revision."""

    expected_version: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=100)


class TaskBoardConflict(BoardModel):
    """A refused mutation and the authoritative current task."""

    detail: str
    current_task: BoardTask
