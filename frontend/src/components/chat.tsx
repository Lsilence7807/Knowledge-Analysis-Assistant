// 文件：frontend/src/components/chat.tsx
// 作用：首页聊天壳——assistant-ui 的 Thread/Composer 接 AI SDK runtime；每个 data 帧交给 frames/results 画
// 阶段：F2a 前端工程
// 依赖：@assistant-ui/react、@assistant-ui/react-ai-sdk、src/lib/stream.ts
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  ThreadPrimitive,
  type ThreadMessage,
} from "@assistant-ui/react";
import { useChatRuntime } from "@assistant-ui/react-ai-sdk";
import { useMemo } from "react";

import { ConclusionCard, DegradedNotice, SqlCollapse, StepsPanel } from "@/components/frames";
import { Results } from "@/components/results";
import {
  AskStreamTransport,
  type AskUIMessage,
  type DegradedFrame,
  type InsightFrame,
  type RowsFrame,
  type SqlFrame,
  type TraceFrame,
} from "@/lib/stream";

type MessagePart = ThreadMessage["content"][number];

// assistant-ui 把 AI SDK 的 data-<name> 部件转成 { type: "data", name, data }
function DataPart({ name, data }: { name: string; data: unknown }) {
  switch (name) {
    case "sql":
      return <SqlCollapse sql={(data as SqlFrame).sql} />;
    case "rows":
      return <Results frame={data as RowsFrame} />;
    case "degraded":
      return <DegradedNotice frame={data as DegradedFrame} />;
    case "insight":
      return <ConclusionCard frame={data as InsightFrame} />;
    case "trace":
      return <StepsPanel frame={data as TraceFrame} />;
    default:
      // plan 与 done 帧是编排信息（开关状态、耗时），界面上没有新内容可画
      return null;
  }
}

function Part({ part }: { part: MessagePart }) {
  if (part.type === "text") return <p className="leading-relaxed whitespace-pre-wrap">{part.text}</p>;
  if (part.type === "data") return <DataPart name={part.name} data={part.data} />;
  return null;
}

function Message({ message }: { message: ThreadMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={isUser ? "flex justify-end" : "block"}>
      <div
        className={isUser ? "max-w-[80%] rounded-lg border-[3px] border-ink bg-yellow px-4 py-2" : "space-y-2"}
      >
        {message.content.map((part, index) => (
          <Part key={index} part={part} />
        ))}
      </div>
    </div>
  );
}

export function Chat() {
  // 传输层只建一次：重建会把已聊的历史冲掉
  const transport = useMemo(() => new AskStreamTransport(), []);
  const runtime = useChatRuntime<AskUIMessage>({ transport });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="flex h-[68vh] min-h-[420px] flex-col gap-3 rounded-lg border-[3px] border-ink bg-card p-4 shadow-hard">
        <ThreadPrimitive.Viewport className="flex-1 space-y-3 overflow-y-auto pr-1">
          <ThreadPrimitive.Empty>
            <p className="text-sm text-muted-foreground">
              问一句关于当前数据集的话，例如「各渠道的销售额排名如何？」
            </p>
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages>
            {({ message }) => <Message message={message} />}
          </ThreadPrimitive.Messages>
        </ThreadPrimitive.Viewport>
        <ComposerPrimitive.Root className="flex items-end gap-2 border-t-[3px] border-ink pt-3">
          <ComposerPrimitive.Input
            rows={2}
            placeholder="提问…（Enter 发送，Shift+Enter 换行）"
            className="w-full resize-none rounded-md border-[3px] border-ink bg-paper px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <ComposerPrimitive.Send className="rounded-md border-[3px] border-ink bg-violet px-4 py-2 text-sm font-semibold text-primary-foreground">
            提问
          </ComposerPrimitive.Send>
        </ComposerPrimitive.Root>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
