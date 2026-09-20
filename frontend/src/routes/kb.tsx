// 文件：frontend/src/routes/kb.tsx
// 作用：知识库页——导入文档目录 + 关键词检索（命中带回出处与不可信标记的片段）
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-query、src/lib/apiClient.ts、sonner
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { fetchJson } from "@/lib/apiClient";

type KbHit = { source: string; title: string; chunk_no: number; score: number; fragment: string };
type KbSearch = { query: string; hits: KbHit[] };

export function KbPage() {
  const [path, setPath] = useState("");
  const [query, setQuery] = useState("");

  const importDoc = useMutation({
    mutationFn: (value: string) =>
      fetchJson<Record<string, unknown>>("/kb/import", {
        method: "POST",
        body: JSON.stringify({ path: value }),
      }),
    onSuccess: () => toast.success("导入完成，详情见下方结果"),
    onError: (error) => toast.error(error.message),
  });
  const search = useMutation({
    mutationFn: (value: string) =>
      fetchJson<KbSearch>("/kb/search", {
        method: "POST",
        body: JSON.stringify({ query: value }),
      }),
    onError: (error) => toast.error(error.message),
  });

  return (
    <div className="space-y-5">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">导入文档</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="kb-path">文档或目录路径（仓库内相对路径）</Label>
            <Input
              id="kb-path"
              value={path}
              placeholder="例如 skills/demo-reference"
              onChange={(event) => setPath(event.target.value)}
            />
          </div>
          <Button disabled={importDoc.isPending || path.trim() === ""} onClick={() => importDoc.mutate(path.trim())}>
            导入
          </Button>
          {importDoc.data && (
            <pre className="max-h-[240px] overflow-auto rounded-md bg-hair px-3 py-2 text-xs">
              {JSON.stringify(importDoc.data, null, 2)}
            </pre>
          )}
        </CardContent>
      </Card>

      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">检索</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-[240px] flex-1 space-y-1">
              <Label htmlFor="kb-query">检索词</Label>
              <Input
                id="kb-query"
                value={query}
                placeholder="例如 退款规则"
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <Button disabled={search.isPending || query.trim() === ""} onClick={() => search.mutate(query.trim())}>
              检索
            </Button>
          </div>
          {search.data?.hits.length === 0 && <p className="text-sm text-muted-foreground">没有命中。</p>}
          {search.data?.hits.map((hit, index) => (
            <div key={index} className="rounded-md border border-ink px-3 py-2 text-sm">
              <div className="font-medium">
                {hit.title}{" "}
                <span className="text-xs text-muted-foreground">
                  第 {hit.chunk_no} 片 · 分数 {hit.score}
                </span>
              </div>
              <div className="text-xs text-muted-foreground">{hit.source}</div>
              <pre className="mt-1 whitespace-pre-wrap text-xs">{hit.fragment}</pre>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
