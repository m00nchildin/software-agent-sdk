# Shared team task board

`TaskBoardTool` uses the Agent Server's persisted, conversation-tree-scoped board.
It is opt-in and works without a browser or WebSocket connection:

```python
from openhands.sdk import Agent, Tool
from openhands.tools.task_board import TaskBoardTool

agent = Agent(llm=llm, tools=[Tool(name=TaskBoardTool.name)])
```

The executor binds requests to the current conversation id and uses the existing
`OH_INTERNAL_SERVER_URL` and session-key contract. All members on the same server
resolve the same board from persisted `parent_conversation_id` links. A missing
ancestor returns 404; children of a deleted root cannot silently create a new board.

## REST contract

The base path is `/api/conversations/{member_id}/task-board`:

| Operation | Method and suffix | Body |
| --- | --- | --- |
| Read the board | `GET` | None |
| Create a task | `POST /tasks` | Complete task contents; `title` is required |
| Replace task contents | `PUT /tasks/{task_id}` | Task contents and `expected_version` |
| Claim for this member | `POST /tasks/{task_id}/claim` | `expected_version`, `role` |

Every operation accepts optional `root_conversation_id`. A root outside the
member's tree is rejected with 403, and task ids from another board are not found.
The existing session key grants server-wide access; this feature does not introduce
per-conversation authentication. Hosts must supply the caller's member id, and the
agent tool derives it from its own conversation state rather than an action field.

A task contains `id`, `version`, `title`, nullable `owner` and `role`, `status`,
`depends_on`, nullable `parallel_group`, `blocked`, and `completion_commit`.
The owner and blocking `by` are UUID conversation ids in the same tree. A role
without an owner is invalid. Unclaimed tasks have no owner. All writes reject extra
fields, and validation errors do not echo submitted values.

Canonical statuses are `todo`, `in_progress`, `done`, and `blocked`. The board
reuses TaskTracker's alias normalization, including `pending` and `completed`;
`doing` is rejected. TaskTracker's accepted statuses are unchanged.
`blocked` requires `{reason, by}` and is cleared when leaving that status.
`done` requires a 7–64 digit hexadecimal `completion_commit`; other states clear
the completion commit. Dependencies must exist, cannot form cycles, and must all
be done before claiming or entering an active/completed state.

An update replaces task contents at the observed revision. It cannot transfer
ownership: release with `owner=null`, `role=null`, `status=todo`, then claim at the
new revision. A stale update or losing/repeated claim returns 409 with
`{detail, current_task}`. Read that state before deciding to retry. A timeout can
occur after a write commits; inspect the board before retrying a create.

## TypeScript

```typescript
import { AgentServerClient } from '@openhands/typescript-client/clients';

const client = new AgentServerClient({ host, apiKey });
const board = await client.taskBoards.read(memberId);
const claimed = await client.taskBoards.claim(memberId, taskId, 'engineer', version);
if ('current_task' in claimed) {
  console.log('Claim refused:', claimed.current_task.owner);
} else {
  const { id, version: expectedVersion, ...contents } = claimed;
  await client.taskBoards.update(
    memberId, id,
    { ...contents, status: 'done', completion_commit: commit },
    expectedVersion
  );
}
client.close();
```

The additive client types describe this new API. The existing generated transport
file remains tied to its published server image; release tooling will regenerate
that pinned contract when a release contains these operations.

## Persistence and runtime boundary

The board is stored at `<conversations>/<root UUID hex>/task_board.json`.
Transactions read current metadata and board contents under one cross-process
file lock outside conversation directories, and persist through the SDK's
fsync-and-atomic-replace helper. Lock waits and file I/O run on worker threads.
Conversation removal uses the same lock. Removing a member retains the board;
removing the root removes it. Unreadable/corrupt data is not reset or overwritten.

REST operations stay on the outer catalog in Docker/hybrid mode. They do not start
containers or require a runtime lease. An isolated container only mounts its own
conversation directory: a tool executed there cannot resolve an outer multi-runtime
tree through its loopback server. Such hosts need a conversation-scoped bridge to
the outer board API; do not forward a server-wide outer session key into containers.
This change does not add that separate bridge or change container isolation.

The board performs no scheduling, mailbox delivery, git operation, or PR creation.
It does not touch Canvas's branch snapshot.
