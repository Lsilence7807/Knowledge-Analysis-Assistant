// 文件：frontend/src/components/frames.tsx
// 作用：流式帧的业务渲染：结论卡片、工具步骤面板、SQL 折叠、降级提示
// 阶段：F2a 前端工程
// 依赖：src/lib/stream.ts（帧结构）、src/components/ui/{card,badge}.tsx
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { DegradedFrame, InsightFrame, TraceFrame } from "@/lib/stream";

const CONFIDENCE_LABEL: Record<string, string> = { low: "低", medium: "中", high: "高" };

function directionIcon(direction: string | undefined): string {
  if (direction === "up") return "↑";
  if (direction === "down") return "↓";
  return "·";
}

export function ConclusionCard({ frame }: { frame: InsightFrame }) {
  // 模型没给出结论时帧里只有 message：把降级原因照实显示，不替模型编一个结论
  if (frame.insight === null) {
    return (
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">结论</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {frame.message || "模型没有给出结论"}
        </CardContent>
      </Card>
    );
  }
  const insight = frame.insight;
  return (
    <Card className="border-[3px] border-ink shadow-hard">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          结论
          <Badge variant="secondary">置信度 {CONFIDENCE_LABEL[insight.confidence] ?? insight.confidence}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="font-semibold">{insight.summary}</p>
        {insight.findings.length > 0 && (
          <ul className="space-y-1">
            {insight.findings.map((finding, index) => (
              <li key={index}>
                <span className="mr-1 text-violet-dark">{directionIcon(finding.direction)}</span>
                <span className="font-medium">{finding.title}</span>
                {finding.detail && <span className="text-muted-foreground">：{finding.detail}</span>}
                {typeof finding.row === "number" && (
                  <span className="ml-1 text-xs text-muted-foreground">（结果表第 {finding.row + 1} 行）</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {insight.suggestions.length > 0 && (
          <div>
            <div className="font-medium">下一步</div>
            <ul className="list-disc pl-5 text-muted-foreground">
              {insight.suggestions.map((item, index) => (
                <li key={index}>
                  {item.action}
                  {item.rationale ? `——${item.rationale}` : ""}
                </li>
              ))}
            </ul>
          </div>
        )}
        {insight.caveats.length > 0 && (
          <div className="rounded-md bg-hair px-3 py-2 text-xs">
            <div className="font-medium">注意</div>
            <ul className="list-disc pl-5">
              {insight.caveats.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function StepsPanel({ frame }: { frame: TraceFrame }) {
  if (frame.steps.length === 0) return null;
  return (
    <details className="rounded-md border border-ink bg-card px-3 py-2 text-sm" open>
      <summary className="cursor-pointer font-medium">
        工具步骤 {frame.steps.length} 步 · {frame.elapsed_ms} ms
        <span className="ml-2 text-xs text-muted-foreground">任务 {frame.task_id}</span>
      </summary>
      <ol className="mt-2 space-y-1">
        {frame.steps.map((step, index) => (
          <li key={index} className="flex flex-wrap gap-2">
            <span className="text-muted-foreground">{index + 1}.</span>
            <span className="font-medium">{String(step.tool ?? "步骤")}</span>
            <span className="text-muted-foreground">
              {step.ok === false ? `失败：${String(step.error ?? "")}` : `耗时 ${String(step.elapsed_ms ?? "-")} ms`}
            </span>
          </li>
        ))}
      </ol>
    </details>
  );
}

export function SqlCollapse({ sql }: { sql: string }) {
  if (!sql) return null;
  return (
    <details className="rounded-md border border-ink bg-card px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium">看 SQL</summary>
      <pre className="mt-2 overflow-x-auto whitespace-pre-wrap font-mono">{sql}</pre>
    </details>
  );
}

export function DegradedNotice({ frame }: { frame: DegradedFrame }) {
  return (
    <p className="rounded-md bg-coral/15 px-3 py-2 text-sm">
      降级（{frame.stage}：{frame.reason}）
      {frame.message ? `——${frame.message}` : ""}
    </p>
  );
}
