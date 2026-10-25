import { afterEach, expect, it, vi } from 'vitest';
import { AgentServerClient } from '../client/openhands-client';
import { TaskBoardClient, type BoardTask } from '../client/task-board-client';
import { HttpError } from '../client/http-client';

afterEach(() => vi.unstubAllGlobals());

it('returns typed current state on a lost claim and does not retry it', async () => {
  const current: BoardTask = {
    id: 'task',
    version: 1,
    title: 'work',
    owner: 'winner',
    role: 'engineer',
    status: 'in_progress',
    depends_on: [],
    parallel_group: null,
    blocked: null,
    completion_commit: null,
  };
  const fetchMock = vi.fn(async () =>
    Response.json({ detail: 'Task version is stale', current_task: current }, { status: 409 })
  );
  vi.stubGlobal('fetch', fetchMock);
  const client = new TaskBoardClient({ host: 'http://board-server', apiKey: 'session' });
  const result = await client.claim('member', 'task', 'engineer', 0, 'root');
  expect(result).toEqual({ detail: 'Task version is stale', current_task: current });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, request] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(String(url)).toBe(
    'http://board-server/api/conversations/member/task-board/tasks/task/claim?root_conversation_id=root'
  );
  expect(request.method).toBe('POST');
  expect(JSON.parse(String(request.body))).toEqual({ expected_version: 0, role: 'engineer' });
  expect(new Headers(request.headers).get('X-Session-API-Key')).toBe('session');
  client.close();
});

it('exposes board read and complete replacement on the aggregate client', async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, request?: RequestInit) => {
    if (request?.method === 'PUT') {
      return Response.json({ title: 'done', status: 'done', completion_commit: 'abcdef1' });
    }
    return Response.json({ root_conversation_id: 'root', tasks: [] });
  });
  vi.stubGlobal('fetch', fetchMock);
  const client = new AgentServerClient({ host: 'http://board-server' });
  expect(await client.taskBoards.read('member')).toEqual({
    root_conversation_id: 'root',
    tasks: [],
  });
  const completed = await client.taskBoards.update(
    'member',
    'task',
    {
      title: 'done',
      owner: 'member',
      role: 'engineer',
      status: 'done',
      completion_commit: 'abcdef1',
    },
    1
  );
  expect(completed).toMatchObject({ status: 'done', completion_commit: 'abcdef1' });
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
    expected_version: 1,
  });
  client.close();
});

it('keeps out-of-tree reads as errors', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ detail: 'out of tree' }, { status: 403 }))
  );
  const client = new TaskBoardClient({ host: 'http://board-server' });
  await expect(client.read('outsider', 'root')).rejects.toBeInstanceOf(HttpError);
  client.close();
});
