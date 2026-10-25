# Shared task board

The Agent Server owns one board per persisted conversation tree. Any member can
read it without starting an agent or attaching a browser. Mutations are durable,
versioned transactions; claims require every dependency to be done. Deleting the
root removes the board under the same cross-process lock as board writes.

Source: `openhands-agent-server/openhands/agent_server/task_board.py`,
`openhands-agent-server/openhands/agent_server/task_board_router.py`,
`openhands-tools/openhands/tools/task_board/`,
`clients/typescript/src/client/task-board-client.ts`

Routes: `GET /api/conversations/{conversation_id}/task-board`,
`POST /api/conversations/{conversation_id}/task-board/tasks`,
`PUT /api/conversations/{conversation_id}/task-board/tasks/{task_id}`,
`POST /api/conversations/{conversation_id}/task-board/tasks/{task_id}/claim`.

## Sub-features

- `F36.shared-state`: a root and member see the same task; an outsider's explicitly targeted read and write are rejected without mutation.
- `F36.claim-cas`: a versioned claim acquires one owner, a repeated claim returns its current state, and completion records a commit.

## How to get to it (agent POV)

REST uses the paths above. The opt-in `TaskBoardTool` binds requests to its own
conversation id. TypeScript exposes `TaskBoardClient` and
`AgentServerClient.taskBoards`; claim/update return typed current state on 409.
The session key retains its existing server-wide authority. The member id scopes
an operation to a tree; it is not a new authentication principal.

Outer Docker/hybrid requests stay on the catalog. An isolated container's loopback
server does not mount the outer tree; a narrowly scoped bridge for agent tools in
such containers is separate work. No outer server-wide key is injected there.

## Driving it with control-agent-server

Preconditions: an authenticated run is launched or attached, exported as
`AGENT_SERVER_VERIFY_RUN`, and `doctor` is ok. No model calls are made. Set
`QA_BOARD_WORKSPACE` to a writable directory inside that run's workspace.

- **Read shared state and reject an outside tree (`F36.shared-state`).**
  Create three idle conversations through the standard API. No conversation is run
  and no browser or WebSocket is connected.

  ```sh
  START="{\"agent\":{\"llm\":{\"model\":\"gpt-4o\",\"usage_id\":\"qa-board\"},\"tools\":[]},\"workspace\":{\"working_dir\":\"$QA_BOARD_WORKSPACE\"}}"
  ROOT=$(control-agent-server api POST /api/conversations --json "$START" --expect 201 --field id)
  OTHER=$(control-agent-server api POST /api/conversations --json "$START" --expect 201 --field id)
  CHILD_START="{\"agent\":{\"llm\":{\"model\":\"gpt-4o\",\"usage_id\":\"qa-board\"},\"tools\":[]},\"workspace\":{\"working_dir\":\"$QA_BOARD_WORKSPACE\"},\"parent_conversation_id\":\"$ROOT\"}"
  MEMBER=$(control-agent-server api POST /api/conversations --json "$CHILD_START" --expect 201 --field id)
  TASK=$(control-agent-server api POST "/api/conversations/$ROOT/task-board/tasks" --json '{"title":"QA shared work"}' --expect 201 --field id)
  control-agent-server api GET "/api/conversations/$MEMBER/task-board" --check tasks.0.id eq "$TASK" --save F36.shared-state/member
  control-agent-server api GET "/api/conversations/$OTHER/task-board" --query "root_conversation_id=$ROOT" --expect 403 --save F36.shared-state/outside-read
  control-agent-server api POST "/api/conversations/$OTHER/task-board/tasks" --query "root_conversation_id=$ROOT" --json '{"title":"QA denied"}' --expect 403 --save F36.shared-state/outside-write
  control-agent-server api GET "/api/conversations/$ROOT/task-board" --check tasks len-eq 1
  ```

- **Compare-and-set claim and completion (`F36.claim-cas`).**
  Requires: `F36.shared-state`.

  ```sh
  control-agent-server api POST "/api/conversations/$MEMBER/task-board/tasks/$TASK/claim" --json '{"expected_version":0,"role":"engineer"}' --expect 200 --check owner eq "$MEMBER" --check version eq 1 --save F36.claim-cas/winner
  control-agent-server api POST "/api/conversations/$ROOT/task-board/tasks/$TASK/claim" --json '{"expected_version":0,"role":"lead"}' --expect 409 --check current_task.owner eq "$MEMBER" --check current_task.version eq 1 --save F36.claim-cas/loser
  DONE="{\"title\":\"QA shared work\",\"owner\":\"$MEMBER\",\"role\":\"engineer\",\"status\":\"done\",\"completion_commit\":\"abcdef1234\",\"expected_version\":1}"
  control-agent-server api PUT "/api/conversations/$MEMBER/task-board/tasks/$TASK" --json "$DONE" --expect 200 --check status eq done --check completion_commit eq abcdef1234 --save F36.claim-cas/completed
  control-agent-server api GET "/api/conversations/$ROOT/task-board" --check tasks.0.status eq done --check tasks.0.completion_commit eq abcdef1234 --save F36.claim-cas/readback
  control-agent-server api DELETE "/api/conversations/$ROOT" --expect 200
  control-agent-server api GET "/api/conversations/$MEMBER/task-board" --expect 404 --save F36.claim-cas/deleted-root
  control-agent-server api DELETE "/api/conversations/$MEMBER" --expect 200
  control-agent-server api DELETE "/api/conversations/$OTHER" --expect 200
  ```

## Gotchas

`tests/agent_server/test_task_board.py` covers competing HTTP requests, two actual
processes synchronized at a barrier, repeated claims, dependency gating and cycles,
blocked/completed transitions, failed writes, corrupt data, scope/auth rejection,
fresh-service recovery, member deletion and root-deletion races.
`tests/cross/test_task_board_live_server.py` runs the actual tool over HTTP, kills
the server process, restarts against the same storage, checks both an outstanding
claim and a completion, and deletes the root. Its 1-second configured runtime
lease lets root deletion proceed after the crash on Windows. Board reads themselves
do not require a runtime lease.
