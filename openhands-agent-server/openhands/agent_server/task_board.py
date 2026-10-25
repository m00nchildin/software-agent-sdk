"""Durable task-board transactions shared by every conversation in a tree."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock
from pydantic import BaseModel

from openhands.agent_server.utils import safe_rmtree
from openhands.sdk.utils.files import atomic_write_text
from openhands.tools.task_board.models import (
    BoardTask,
    BoardTaskInput,
    ClaimBoardTaskRequest,
    TaskBoard,
    UpdateBoardTaskRequest,
)


class TaskBoardError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class TaskConflict(TaskBoardError):
    def __init__(self, detail: str, task: BoardTask) -> None:
        super().__init__(409, detail)
        self.current_task = task


class _TreeNode(BaseModel):
    id: UUID
    parent_conversation_id: UUID | None = None


class TaskBoardStore:
    """Serialize board mutations and conversation removal across processes.

    Metadata is read from disk inside each transaction, without hydrating an agent.
    The lock lives outside conversation directories so deletion cannot replace it
    while another process is waiting. File I/O must run off the async event loop.
    """

    def __init__(self, conversations_dir: Path) -> None:
        self.conversations_dir = conversations_dir
        self.lock_path = conversations_dir / ".task-board.lock"

    def _root(self, conversation_id: UUID) -> UUID:
        seen: set[UUID] = set()
        while conversation_id not in seen:
            seen.add(conversation_id)
            path = self.conversations_dir / conversation_id.hex / "meta.json"
            try:
                node = _TreeNode.model_validate_json(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise TaskBoardError(404, "Conversation tree not found") from exc
            if node.id != conversation_id:
                raise TaskBoardError(404, "Conversation tree not found")
            if node.parent_conversation_id is None:
                return conversation_id
            conversation_id = node.parent_conversation_id
        raise TaskBoardError(500, "Conversation tree contains a cycle")

    def _load(self, member: UUID, root: UUID | None) -> TaskBoard:
        actual_root = self._root(member)
        if root is not None and root != actual_root:
            raise TaskBoardError(403, "Conversation is outside the board's tree")
        path = self.conversations_dir / actual_root.hex / "task_board.json"
        if not path.exists():
            return TaskBoard(root_conversation_id=actual_root)
        board = TaskBoard.model_validate_json(path.read_text(encoding="utf-8"))
        if board.root_conversation_id != actual_root:
            raise TaskBoardError(500, "Task board root does not match its tree")
        return board

    def read(self, member: UUID, root: UUID | None = None) -> TaskBoard:
        with FileLock(self.lock_path):
            return self._load(member, root)

    def _save(self, board: TaskBoard) -> None:
        path = (
            self.conversations_dir / board.root_conversation_id.hex / "task_board.json"
        )
        atomic_write_text(path, board.model_dump_json(indent=2))

    def _validate_task(self, board: TaskBoard, task: BoardTask) -> None:
        for identity in (task.owner, task.blocked.by if task.blocked else None):
            if (
                identity is not None
                and self._root(identity) != board.root_conversation_id
            ):
                raise TaskBoardError(403, "Task owner or blocker is outside the tree")
        tasks = {item.id: item for item in board.tasks}
        tasks[task.id] = task
        for dependency in task.depends_on:
            if dependency not in tasks:
                raise TaskBoardError(422, "Unknown task dependency")
        pending = list(task.depends_on)
        seen: set[UUID] = set()
        while pending:
            dependency = pending.pop()
            if dependency == task.id:
                raise TaskBoardError(422, "Task dependencies contain a cycle")
            if dependency not in seen:
                seen.add(dependency)
                pending.extend(tasks[dependency].depends_on)
        if task.status in ("in_progress", "done"):
            if any(tasks[dep].status != "done" for dep in task.depends_on):
                raise TaskBoardError(422, "Task dependencies are not done")

    @staticmethod
    def _require_dependencies(board: TaskBoard, task: BoardTask) -> None:
        tasks = {item.id: item for item in board.tasks}
        if any(tasks[dep].status != "done" for dep in task.depends_on):
            raise TaskConflict("Task dependencies are not done", task)

    def create(
        self, member: UUID, request: BoardTaskInput, root: UUID | None = None
    ) -> BoardTask:
        with FileLock(self.lock_path):
            board = self._load(member, root)
            task = BoardTask(id=uuid4(), version=0, **request.model_dump())
            self._validate_task(board, task)
            board.tasks.append(task)
            self._save(board)
            return task

    def _mutate(
        self,
        member: UUID,
        task_id: UUID,
        expected_version: int,
        change: Callable[[BoardTask, TaskBoard], BoardTask],
        root: UUID | None,
    ) -> BoardTask:
        with FileLock(self.lock_path):
            board = self._load(member, root)
            for index, current in enumerate(board.tasks):
                if current.id != task_id:
                    continue
                if current.version != expected_version:
                    raise TaskConflict("Task version is stale", current)
                updated = change(current, board)
                self._validate_task(board, updated)
                board.tasks[index] = updated
                self._save(board)
                return updated
            raise TaskBoardError(404, "Task not found")

    def update(
        self,
        member: UUID,
        task_id: UUID,
        request: UpdateBoardTaskRequest,
        root: UUID | None = None,
    ) -> BoardTask:
        def change(current: BoardTask, _board: TaskBoard) -> BoardTask:
            if request.owner is not None and request.owner != current.owner:
                raise TaskConflict("Use claim to acquire task ownership", current)
            if (
                current.owner is not None
                and request.owner is None
                and request.status != "todo"
            ):
                raise TaskConflict("Release a task by returning it to todo", current)
            return BoardTask(
                id=current.id,
                version=current.version + 1,
                **request.model_dump(exclude={"expected_version"}),
            )

        return self._mutate(member, task_id, request.expected_version, change, root)

    def claim(
        self,
        member: UUID,
        task_id: UUID,
        request: ClaimBoardTaskRequest,
        root: UUID | None = None,
    ) -> BoardTask:
        def change(current: BoardTask, board: TaskBoard) -> BoardTask:
            if current.owner is not None or current.status != "todo":
                raise TaskConflict("Task is already owned or is not todo", current)
            self._require_dependencies(board, current)
            return BoardTask.model_validate(
                {
                    **current.model_dump(),
                    "owner": member,
                    "role": request.role,
                    "status": "in_progress",
                    "version": current.version + 1,
                }
            )

        return self._mutate(member, task_id, request.expected_version, change, root)

    def remove_conversation(self, conversation_id: UUID) -> bool:
        """Delete state under the same lock as board writes, including its board."""
        with FileLock(self.lock_path):
            return safe_rmtree(self.conversations_dir / conversation_id.hex)
