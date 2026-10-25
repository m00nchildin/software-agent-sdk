import { HttpClient } from './http-client';

export type BoardTaskStatus = 'todo' | 'in_progress' | 'done' | 'blocked';

export interface TaskBlocker {
  reason: string;
  by: string;
}

export interface BoardTask {
  id: string;
  version: number;
  title: string;
  owner: string | null;
  role: string | null;
  status: BoardTaskStatus;
  depends_on: string[];
  parallel_group: string | null;
  blocked: TaskBlocker | null;
  completion_commit: string | null;
}

export type BoardTaskInput = Pick<BoardTask, 'title'> &
  Partial<Omit<BoardTask, 'id' | 'version' | 'title' | 'status'>> & {
    status?: BoardTaskStatus | 'pending' | 'to_do' | 'completed' | 'complete';
  };

export interface TaskBoard {
  root_conversation_id: string;
  tasks: BoardTask[];
}

export interface TaskBoardConflict {
  detail: string;
  current_task: BoardTask;
}

export interface TaskBoardClientOptions {
  host: string;
  apiKey?: string;
  timeout?: number;
}

/** Server-owned shared boards, addressed through a member of the conversation tree. */
export class TaskBoardClient {
  private readonly client: HttpClient;

  constructor(options: TaskBoardClientOptions) {
    this.client = new HttpClient({
      baseUrl: options.host,
      apiKey: options.apiKey,
      timeout: options.timeout ?? 60000,
    });
  }

  async read(conversationId: string, rootConversationId?: string): Promise<TaskBoard> {
    const response = await this.client.get<TaskBoard>(this.path(conversationId), {
      params: { root_conversation_id: rootConversationId },
    });
    return response.data;
  }

  async create(
    conversationId: string,
    task: BoardTaskInput,
    rootConversationId?: string
  ): Promise<BoardTask> {
    const response = await this.client.post<BoardTask>(`${this.path(conversationId)}/tasks`, task, {
      params: { root_conversation_id: rootConversationId },
    });
    return response.data;
  }

  /** A 409 is returned as typed current state; callers must decide before retrying. */
  async update(
    conversationId: string,
    taskId: string,
    task: BoardTaskInput,
    expectedVersion: number,
    rootConversationId?: string
  ): Promise<BoardTask | TaskBoardConflict> {
    const response = await this.client.put<BoardTask | TaskBoardConflict>(
      `${this.path(conversationId)}/tasks/${encodeURIComponent(taskId)}`,
      { ...task, expected_version: expectedVersion },
      {
        params: { root_conversation_id: rootConversationId },
        acceptableStatusCodes: new Set([200, 409]),
      }
    );
    return response.data;
  }

  async claim(
    conversationId: string,
    taskId: string,
    role: string,
    expectedVersion: number,
    rootConversationId?: string
  ): Promise<BoardTask | TaskBoardConflict> {
    const response = await this.client.post<BoardTask | TaskBoardConflict>(
      `${this.path(conversationId)}/tasks/${encodeURIComponent(taskId)}/claim`,
      { role, expected_version: expectedVersion },
      {
        params: { root_conversation_id: rootConversationId },
        acceptableStatusCodes: new Set([200, 409]),
      }
    );
    return response.data;
  }

  close(): void {
    this.client.close();
  }

  private path(conversationId: string): string {
    return `/api/conversations/${encodeURIComponent(conversationId)}/task-board`;
  }
}
