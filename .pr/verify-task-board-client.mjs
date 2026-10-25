import assert from 'node:assert/strict';
import { AgentServerClient } from '../clients/typescript/dist/clients.js';

const client = new AgentServerClient({
  host: process.env.TASK_BOARD_SERVER_URL,
  apiKey: process.env.TASK_BOARD_API_KEY,
});
const ids = [];
async function start(parent) {
  const conversation = await client.post('/api/conversations', {
    agent: { llm: { model: 'gpt-4o', usage_id: 'qa-board-client' }, tools: [] },
    workspace: { working_dir: process.env.TASK_BOARD_WORKSPACE },
    parent_conversation_id: parent,
  });
  ids.push(conversation.id);
  return conversation.id;
}
try {
  const root = await start();
  const first = await start(root);
  const second = await start(root);
  const outside = await start();
  const task = await client.taskBoards.create(root, { title: 'QA typed client' });
  const results = await Promise.all([
    client.taskBoards.claim(first, task.id, 'engineer', 0),
    client.taskBoards.claim(second, task.id, 'engineer', 0),
  ]);
  const won = results.filter((result) => !('current_task' in result));
  const lost = results.filter((result) => 'current_task' in result);
  assert.equal(won.length, 1);
  assert.equal(lost.length, 1);
  assert.deepEqual(lost[0].current_task, won[0]);
  assert.deepEqual((await client.taskBoards.read(second)).tasks, won);
  await assert.rejects(client.taskBoards.read(outside, root), { status: 403 });
  const { id, version, ...contents } = won[0];
  const completed = await client.taskBoards.update(
    first, id, { ...contents, status: 'done', completion_commit: 'abcdef1234' }, version
  );
  assert.equal(completed.status, 'done');
  assert.equal(completed.completion_commit, 'abcdef1234');
  const readback = await client.taskBoards.read(root);
  assert.deepEqual(readback.tasks, [completed]);
  await client.delete(`/api/conversations/${root}`);
  ids.splice(ids.indexOf(root), 1);
  await assert.rejects(client.taskBoards.read(first), { status: 404 });
  console.log(JSON.stringify({
    result: 'pass', transport: 'built TypeScript client over real HTTP',
    competing_claims: { winners: won.length, losers: lost.length },
    completion: completed, readback,
    outside_tree: 403, after_root_delete: 404,
  }, null, 2));
} finally {
  for (const id of ids) await client.delete(`/api/conversations/${id}`);
  client.close();
}
