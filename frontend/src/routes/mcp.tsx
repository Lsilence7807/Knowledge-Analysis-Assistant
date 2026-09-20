// 文件：frontend/src/routes/mcp.tsx
// 作用：MCP 页——列 server 工具（标出白名单内与否）+ 调用一个白名单工具
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-query、src/lib/apiClient.ts、sonner
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { fetchJson } from "@/lib/apiClient";

// ponytail: /mcp/tools 还没挂 response_model；字段照抄 app/providers/mcp_client.py 的 list_tools
type McpTool = { name: string; description: string; allowed: boolean; schema: unknown };
type McpTools = { command: string; tools: McpTool[] };

function parseArgs(text: string): unknown {
  try {
    return JSON.parse(text.trim() === "" ? "{}" : text);
  } catch {
    throw new Error("参数不是合法 JSON");
  }
}

export function McpPage() {
  const [tool, setTool] = useState("");
  const [args, setArgs] = useState("{}");
  const tools = useQuery({
    queryKey: ["mcp-tools"],
    queryFn: () => fetchJson<McpTools>("/mcp/tools"),
    retry: false,
  });
  const call = useMutation({
    mutationFn: () =>
      fetchJson<Record<string, unknown>>("/mcp/call", {
        method: "POST",
        body: JSON.stringify({ tool, arguments: parseArgs(args) }),
      }),
    onError: (error) => toast.error(error.message),
  });

  return (
    <div className="space-y-5">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            MCP 工具
            {tools.data && <span className="font-mono text-xs font-normal text-muted-foreground">{tools.data.command}</span>}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {tools.isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
          {tools.isError && <p className="text-sm text-coral">读不出来：{tools.error?.message ?? ""}</p>}
          {(tools.data?.tools.length ?? 0) > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>工具</TableHead>
                  <TableHead>说明</TableHead>
                  <TableHead>白名单</TableHead>
                  <TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tools.data?.tools.map((item) => (
                  <TableRow key={item.name}>
                    <TableCell className="font-mono text-xs">{item.name}</TableCell>
                    <TableCell>{item.description}</TableCell>
                    <TableCell>
                      <Badge variant={item.allowed ? "default" : "secondary"}>
                        {item.allowed ? "已允许" : "未允许"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Button size="sm" variant="outline" onClick={() => setTool(item.name)}>
                        选中
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">调用工具</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="mcp-tool">工具名</Label>
            <Input id="mcp-tool" value={tool} placeholder="从上面表里选一个" onChange={(event) => setTool(event.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="mcp-args">参数（JSON）</Label>
            <Textarea id="mcp-args" rows={4} value={args} onChange={(event) => setArgs(event.target.value)} />
          </div>
          <Button disabled={call.isPending || tool.trim() === ""} onClick={() => call.mutate()}>
            调用
          </Button>
          {call.data && (
            <pre className="max-h-[320px] overflow-auto rounded-md bg-hair px-3 py-2 text-xs">
              {JSON.stringify(call.data, null, 2)}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
