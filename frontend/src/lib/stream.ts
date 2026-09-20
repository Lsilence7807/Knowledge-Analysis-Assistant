// 文件：frontend/src/lib/stream.ts
// 作用：把 §4.11 的 SSE 帧（GET /ask/stream）翻译成 AI SDK 的 UIMessageChunk 流，供 assistant-ui 聊天壳消费；
//       流式未启用（503）时自动回落 POST /ask，把整段结果拼成同一种帧
// 阶段：F2a 前端工程
// 依赖：ai（ChatTransport / UIMessageChunk 类型）、src/lib/apiClient.ts、src/lib/dataset.ts
import type { ChatTransport, UIMessage, UIMessageChunk } from "ai";

import { fetchJson } from "@/lib/apiClient";
import { getDatasetId } from "@/lib/dataset";

// ---- §4.11 的帧载荷：结果表只在这条流里出现，表格与图表都从 rows 帧取数 ----
export type PlanFrame = { question: string; dataset_id: string; agent?: boolean };
export type SqlFrame = { sql: string };
export type RowsFrame = {
  row_count: number;
  truncated?: boolean;
  columns: string[];
  rows: unknown[][];
};
export type DegradedFrame = { stage: string; reason: string; message?: string };
export type InsightFrame = { insight: InsightPayload | null; text: string; message?: string };
export type TraceFrame = { task_id: string; steps: AgentStep[]; elapsed_ms: number };
export type DoneFrame = { task_id: string; degraded: string[]; message?: string; elapsed_ms: number };

// 结论与步骤的结构由后端 pydantic 模型定义（app/schemas.py、app/agent.py）。
// ponytail: 路由还没挂 response_model，/openapi.json 里没有这两块结构；F1 挂上响应模型后换成生成类型
export type InsightPayload = {
  summary: string;
  findings: FindingPayload[];
  anomalies: Record<string, unknown>[];
  suggestions: { action: string; rationale?: string }[];
  confidence: "low" | "medium" | "high";
  caveats: string[];
};
export type FindingPayload = {
  title: string;
  detail?: string;
  metric?: string;
  direction?: string;
  row?: number | null;
  column?: string;
};
export type AgentStep = {
  tool?: string;
  ok?: boolean;
  elapsed_ms?: number;
  error?: string;
  [key: string]: unknown;
};

export type AskData = {
  plan: PlanFrame;
  sql: SqlFrame;
  rows: RowsFrame;
  degraded: DegradedFrame;
  insight: InsightFrame;
  trace: TraceFrame;
  done: DoneFrame;
};
export type AskUIMessage = UIMessage<unknown, AskData>;

type SseFrame = { event: string; data: unknown };

/**
 * 传输层：assistant-ui 的 runtime 只认 ChatTransport；§4.11 到 AI SDK 的适配血肉全在这一个类里。
 */
export class AskStreamTransport implements ChatTransport<AskUIMessage> {
  async sendMessages({
    messages,
    abortSignal,
  }: Parameters<
    ChatTransport<AskUIMessage>["sendMessages"]
  >[0]): Promise<ReadableStream<UIMessageChunk>> {
    const question = lastUserText(messages);
    if (!question) throw new Error("问题不能为空");
    const datasetId = getDatasetId();
    if (!datasetId) throw new Error("先在数据集页选一个数据集再提问");

    const url = new URL("/ask/stream", window.location.origin);
    url.searchParams.set("question", question);
    url.searchParams.set("dataset_id", datasetId);

    const res = await fetch(url, {
      headers: { accept: "text/event-stream" },
      signal: abortSignal,
    });
    // §4.11：ENABLE_STREAM=false 时路由回 503，前端自动回落整段返回，不把 503 甩给用户
    if (res.status === 503) return toChunks(fallbackFrames(question, datasetId, abortSignal));
    if (!res.ok || res.body === null) throw new Error(await errorText(res));
    return toChunks(readFrames(res.body));
  }

  /** 断连不续传：§4.11 要求断连即取消上游调用，重进页面重新提问即可。 */
  async reconnectToStream(): Promise<ReadableStream<UIMessageChunk> | null> {
    return null;
  }
}

type AskBody = {
  task_id?: string;
  sql?: string;
  row_count?: number;
  truncated?: boolean;
  columns?: string[];
  rows?: unknown[][];
  insight?: InsightPayload | null;
  message?: string;
  degraded?: string[];
  steps?: AgentStep[];
};

/** 整段回落：POST /ask 的响应拼成与流式同名的帧，下游只认一种帧，不必写第二套渲染。 */
async function* fallbackFrames(
  question: string,
  datasetId: string,
  signal: AbortSignal | undefined,
): AsyncGenerator<SseFrame> {
  const body = await fetchJson<AskBody>("/ask", {
    method: "POST",
    body: JSON.stringify({ dataset_id: datasetId, question }),
    signal,
  });
  yield { event: "plan", data: { question, dataset_id: datasetId } };
  if (body.sql) yield { event: "sql", data: { sql: body.sql } };
  yield {
    event: "row_count",
    data: {
      row_count: body.row_count ?? 0,
      truncated: body.truncated ?? false,
      columns: body.columns ?? [],
      rows: body.rows ?? [],
    },
  };
  for (const reason of body.degraded ?? []) {
    yield { event: "degraded", data: { stage: "query", reason } };
  }
  if (body.message) yield { event: "token", data: { text: body.message } };
  yield {
    event: "insight",
    data: { insight: body.insight ?? null, text: "", message: body.message ?? "" },
  };
  yield {
    event: "trace",
    data: { task_id: body.task_id ?? "", steps: body.steps ?? [], elapsed_ms: 0 },
  };
  yield {
    event: "done",
    data: {
      task_id: body.task_id ?? "",
      degraded: body.degraded ?? [],
      message: body.message ?? "",
      elapsed_ms: 0,
    },
  };
}

function toChunks(frames: AsyncIterable<SseFrame>): ReadableStream<UIMessageChunk> {
  const chunks = mapFrames(frames);
  return new ReadableStream<UIMessageChunk>({
    async pull(controller) {
      const { value, done } = await chunks.next();
      if (done) controller.close();
      else controller.enqueue(value);
    },
    async cancel() {
      await chunks.return(undefined);
    },
  });
}

/** 帧 → AI SDK 部件：文字走 text-* ，其余每种帧一个 data-<name> 部件。 */
async function* mapFrames(frames: AsyncIterable<SseFrame>): AsyncGenerator<UIMessageChunk> {
  let textId: string | null = null;
  yield { type: "start" };
  for await (const frame of frames) {
    switch (frame.event) {
      case "plan":
        yield { type: "data-plan", data: frame.data as PlanFrame };
        break;
      case "sql":
        yield { type: "data-sql", data: frame.data as SqlFrame };
        break;
      case "row_count":
        yield { type: "data-rows", data: frame.data as RowsFrame };
        break;
      case "token": {
        const { text } = frame.data as { text: string };
        if (textId === null) {
          textId = "answer";
          yield { type: "text-start", id: textId };
        }
        yield { type: "text-delta", id: textId, delta: text };
        break;
      }
      case "degraded":
        yield { type: "data-degraded", data: frame.data as DegradedFrame };
        break;
      case "insight":
        yield { type: "data-insight", data: frame.data as InsightFrame };
        break;
      case "trace":
        yield { type: "data-trace", data: frame.data as TraceFrame };
        break;
      case "done":
        yield { type: "data-done", data: frame.data as DoneFrame };
        break;
      default:
        break;
    }
  }
  if (textId !== null) yield { type: "text-end", id: textId };
  yield { type: "finish" };
}

/** SSE 读帧：event/data 两行一组、空行收尾；半个帧由 buffer 兜住，心跳注释行直接丢。 */
async function* readFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut = buffer.indexOf("\n\n");
      while (cut >= 0) {
        const parsed = parseFrame(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (parsed !== null) yield parsed;
        cut = buffer.indexOf("\n\n");
      }
    }
  } finally {
    // 用户点「停止」或关页面时 abortSignal 触发，这里把读取端也关掉，别吊着连接
    await reader.cancel().catch(() => undefined);
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) as unknown };
  } catch {
    return null; // 坏帧不打断整条流：丢一帧总比整段报错强
  }
}

function lastUserText(messages: AskUIMessage[]): string {
  const last = [...messages].reverse().find((message) => message.role === "user");
  if (last === undefined) return "";
  return last.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim();
}

async function errorText(res: Response): Promise<string> {
  const body: unknown = await res.json().catch(() => null);
  if (body !== null && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return `提问失败：HTTP ${res.status}`;
}
