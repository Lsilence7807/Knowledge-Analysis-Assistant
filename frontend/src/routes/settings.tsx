// 文件：frontend/src/routes/settings.tsx
// 作用：设置页——模型（API）配置面板 + 各能力开关状态；这是首页腾出来的那块配置血肉
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-query、src/lib/apiClient.ts、sonner
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, fetchJson } from "@/lib/apiClient";

// ponytail: /settings/models 还没挂 response_model；字段照抄 app/main.py 的 _public_model
type ModelProfile = { id: string; provider: string; base_url: string; model: string; has_key: boolean };
type ModelsResponse = { models: ModelProfile[] };

export function SettingsPage() {
  const client = useQueryClient();
  const [form, setForm] = useState({ id: "", base_url: "", model: "", api_key: "" });

  // /capabilities 有响应模型，直接吃生成的类型
  const caps = useQuery({
    queryKey: ["capabilities"],
    queryFn: async () => {
      const res = await api.GET("/capabilities");
      // 生成的类型里 /capabilities 没声明错误响应，所以只判 data 在不在
      if (res.data === undefined) throw new Error(`能力清单读取失败：HTTP ${res.response.status}`);
      return res.data;
    },
  });
  const models = useQuery({ queryKey: ["models"], queryFn: () => fetchJson<ModelsResponse>("/settings/models") });
  const save = useMutation({
    mutationFn: () =>
      fetchJson<ModelsResponse>("/settings/models", {
        method: "PUT",
        body: JSON.stringify({
          id: form.id.trim(),
          base_url: form.base_url.trim(),
          model: form.model.trim(),
          api_key: form.api_key,
        }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["models"] });
      void client.invalidateQueries({ queryKey: ["capabilities"] });
      setForm({ id: "", base_url: "", model: "", api_key: "" });
      toast.success("已保存并设为默认模型");
    },
    onError: (error) => toast.error(error.message),
  });

  const ready = form.id.trim() !== "" && form.base_url.trim() !== "" && form.model.trim() !== "";

  return (
    <div className="space-y-5">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">能力开关</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {caps.isError && <p className="text-sm text-coral">读不出来：{caps.error?.message ?? ""}</p>}
          <div className="flex flex-wrap gap-2">
            {Object.entries(caps.data?.capabilities ?? {}).map(([name, ok]) => (
              <Badge key={name} variant={ok ? "default" : "secondary"}>
                {name}
                {ok ? " 可用" : " 未启用"}
              </Badge>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            开关来自后端环境变量（ENABLE_KB / ENABLE_MCP / ENABLE_STREAM …），改完要重启服务。
          </p>
        </CardContent>
      </Card>

      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">模型配置</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="model-id">配置 id</Label>
              <Input
                id="model-id"
                value={form.id}
                placeholder="例如 gpt-4o-mini"
                onChange={(event) => setForm({ ...form, id: event.target.value })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="model-name">模型名</Label>
              <Input
                id="model-name"
                value={form.model}
                placeholder="例如 gpt-4o-mini"
                onChange={(event) => setForm({ ...form, model: event.target.value })}
              />
            </div>
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="base-url">Base URL（OpenAI 兼容）</Label>
              <Input
                id="base-url"
                value={form.base_url}
                placeholder="https://api.openai.com/v1"
                onChange={(event) => setForm({ ...form, base_url: event.target.value })}
              />
            </div>
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="api-key">API Key（只写进 config/local.json，不回显）</Label>
              <Input
                id="api-key"
                type="password"
                value={form.api_key}
                onChange={(event) => setForm({ ...form, api_key: event.target.value })}
              />
            </div>
          </div>
          <Button disabled={!ready || save.isPending} onClick={() => save.mutate()}>
            保存并设为默认
          </Button>
          <p className="text-xs text-muted-foreground">
            没配密钥也能用：提问只出结果表与图表，结论走降级提示。
          </p>
        </CardContent>
      </Card>

      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">已配置模型</CardTitle>
        </CardHeader>
        <CardContent>
          {models.isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
          {models.isError && <p className="text-sm text-coral">读不出来：{models.error?.message ?? ""}</p>}
          {(models.data?.models.length ?? 0) > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>id</TableHead>
                  <TableHead>模型</TableHead>
                  <TableHead>Base URL</TableHead>
                  <TableHead>密钥</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {models.data?.models.map((model) => (
                  <TableRow key={model.id}>
                    <TableCell className="font-mono text-xs">{model.id}</TableCell>
                    <TableCell>{model.model}</TableCell>
                    <TableCell className="text-xs">{model.base_url}</TableCell>
                    <TableCell>
                      <Badge variant={model.has_key ? "default" : "secondary"}>
                        {model.has_key ? "已配置" : "未配置"}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
