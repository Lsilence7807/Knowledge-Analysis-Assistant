// 文件：frontend/src/routes/evals.tsx
// 作用：评测页：跑一轮 golden 集并展示最近几轮的指标与基线差值（F8 接上 /evals/* 端点）
// 阶段：F2a 前端工程（占位）；F8 观测与评测接真实端点
// 依赖：@tanstack/react-query、sonner、src/lib/apiClient.ts、src/components/ui/{badge,button,card,table}.tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchJson } from "@/lib/apiClient";

// ponytail: /evals/* 还没挂 response_model，类型先写在页面里；挂上模型后换成生成的类型
type EvalRun = {
  id: number;
  golden_path: string;
  total: number;
  sql_pass_rate: number;
  fidelity_rate: number;
  degrade_rate: number;
  cost_total: number;
  baseline_delta: Record<string, number>;
  created_at: string;
};
type Report = { baseline: Record<string, number>; runs: EvalRun[] };

const BASELINE_KEYS = ["sql_pass_rate", "fidelity_rate", "degrade_rate", "cost_per_task"] as const;

function rate(value: number | undefined): string {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "-";
}

export function EvalsPage() {
  const client = useQueryClient();
  const report = useQuery({ queryKey: ["evals"], queryFn: () => fetchJson<Report>("/evals/report") });
  const run = useMutation({
    mutationFn: () => fetchJson<{ total: number; breached: string[] }>("/evals/run", { method: "POST" }),
    onSuccess: async (data) => {
      if (data.breached.length) {
        toast.error(`跑完 ${data.total} 条，低于基线：${data.breached.join("、")}`);
      } else {
        toast.success(`跑完 ${data.total} 条，全部达标`);
      }
      await client.invalidateQueries({ queryKey: ["evals"] });
    },
    onError: (error) => toast.error(`评测跑不起来：${String(error)}`),
  });

  const latest = report.data?.runs[0];
  const baseline = report.data?.baseline ?? {};

  return (
    <div className="space-y-4">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="text-base">评测</CardTitle>
          <Button disabled={run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? "跑批中…" : "跑一轮 golden"}
          </Button>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {report.isLoading ? <p className="text-muted-foreground">读取中…</p> : null}
          {report.isError ? <p className="text-destructive">读不到评测记录：{String(report.error)}</p> : null}
          {latest ? (
            <div className="flex flex-wrap gap-2">
              <Badge variant="secondary">用例 {latest.total}</Badge>
              <Badge variant="secondary">SQL 通过率 {rate(latest.sql_pass_rate)}</Badge>
              <Badge variant="secondary">fidelity {rate(latest.fidelity_rate)}</Badge>
              <Badge variant={latest.degrade_rate > (baseline.degrade_rate ?? 0) ? "destructive" : "secondary"}>
                降级率 {rate(latest.degrade_rate)}
              </Badge>
              <Badge variant="secondary">成本 {latest.cost_total.toFixed(4)}</Badge>
            </div>
          ) : (
            <p className="text-muted-foreground">还没有跑过评测；点右上角跑一轮（低于基线会标红）。</p>
          )}
          <p className="text-muted-foreground">
            基线：{BASELINE_KEYS.map((key) => `${key} ${baseline[key] ?? "-"}`).join("｜")}
          </p>
        </CardContent>
      </Card>

      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">历史记录</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>用例</TableHead>
                <TableHead>SQL 通过率</TableHead>
                <TableHead>fidelity</TableHead>
                <TableHead>降级率</TableHead>
                <TableHead>与基线</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(report.data?.runs ?? []).map((item) => (
                <TableRow key={item.id}>
                  <TableCell className="text-xs">{item.created_at}</TableCell>
                  <TableCell>{item.total}</TableCell>
                  <TableCell>{rate(item.sql_pass_rate)}</TableCell>
                  <TableCell>{rate(item.fidelity_rate)}</TableCell>
                  <TableCell>{rate(item.degrade_rate)}</TableCell>
                  <TableCell className={item.baseline_delta?.fidelity_rate < 0 ? "text-destructive" : ""}>
                    {typeof item.baseline_delta?.fidelity_rate === "number"
                      ? `fidelity ${(item.baseline_delta.fidelity_rate * 100).toFixed(1)}%`
                      : "-"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}