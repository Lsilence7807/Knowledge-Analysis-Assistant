// 文件：frontend/src/components/results.tsx
// 作用：结果表与图表——两者都只吃 row_count 帧（§4.11：前端只有这一条流，不再多打一次 /query）
// 阶段：F2a 前端工程
// 依赖：echarts-for-react、src/components/ui/{table,tabs}.tsx、src/lib/stream.ts
import type { EChartsOption } from "echarts";
import ReactECharts from "echarts-for-react";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { RowsFrame } from "@/lib/stream";

// 与 P5.1 的 :root 同值，只在 CSS 变量读不到时兜底
const FALLBACK_COLORS = ["#6B4BF6", "#FFD34D", "#FF6A54", "#2F9E5F", "#5433D6"];

function cssVar(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/** 图表跟着结果表走：第一列当分类轴，前三个数值列当系列；没有数值列就不画。 */
function chartOption(columns: string[], rows: unknown[][], numeric: string[]): EChartsOption {
  const ink = cssVar("--ink-soft", "#443C31");
  const palette = ["--violet", "--yellow", "--coral", "--mint", "--violet-dark"].map((name, index) =>
    cssVar(name, FALLBACK_COLORS[index]),
  );
  return {
    color: palette,
    tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: { color: ink } },
    grid: { left: 12, right: 18, top: 34, bottom: 6, containLabel: true },
    xAxis: {
      type: "category",
      data: rows.map((row) => cell(row[0])),
      axisLabel: { color: ink, rotate: rows.length > 12 ? 30 : 0 },
    },
    yAxis: { type: "value", axisLabel: { color: ink } },
    series: numeric.map((name) => ({
      name,
      type: "bar" as const,
      data: rows.map((row) => toNumber(row[columns.indexOf(name)])),
    })),
  };
}

export function Results({ frame }: { frame: RowsFrame }) {
  const columns = frame.columns.map(String);
  const rows = frame.rows;
  const numeric = columns.filter((_name, index) => rows.some((row) => typeof row[index] === "number"));

  return (
    <Tabs defaultValue="chart" className="rounded-lg border-[3px] border-ink bg-card p-3 shadow-hard">
      <TabsList>
        <TabsTrigger value="chart">图表</TabsTrigger>
        <TabsTrigger value="table">
          结果表 {frame.row_count} 行{frame.truncated ? "（已截断）" : ""}
        </TabsTrigger>
      </TabsList>
      <TabsContent value="chart" className="pt-3">
        {numeric.length === 0 ? (
          <p className="text-sm text-muted-foreground">结果里没有数值列，画不了图，请看结果表。</p>
        ) : (
          <ReactECharts
            option={chartOption(columns, rows, numeric.slice(0, 3))}
            style={{ height: 320 }}
            notMerge
          />
        )}
      </TabsContent>
      <TabsContent value="table" className="pt-3">
        <div className="max-h-[360px] overflow-auto rounded-md border border-ink">
          <Table>
            <TableHeader>
              <TableRow>
                {columns.map((name) => (
                  <TableHead key={name} className="whitespace-nowrap">
                    {name}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row, rowIndex) => (
                <TableRow key={rowIndex}>
                  {columns.map((name, columnIndex) => (
                    <TableCell key={name} className="whitespace-nowrap">
                      {cell(row[columnIndex])}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </TabsContent>
    </Tabs>
  );
}
