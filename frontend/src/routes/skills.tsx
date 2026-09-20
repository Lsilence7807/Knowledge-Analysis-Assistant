// 文件：frontend/src/routes/skills.tsx
// 作用：技能页——已导入技能清单 + 从目录导入 SKILL.md
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
import { fetchJson } from "@/lib/apiClient";

// ponytail: /skills 还没挂 response_model；字段照抄 app/store.py 的 list_skills
type Skill = {
  id: number;
  slug: string;
  path: string;
  description: string;
  kind: string;
  enabled: number;
  created_at: string;
};

export function SkillsPage() {
  const client = useQueryClient();
  const [path, setPath] = useState("");
  const list = useQuery({ queryKey: ["skills"], queryFn: () => fetchJson<{ skills: Skill[] }>("/skills") });
  const importDir = useMutation({
    mutationFn: (value: string) =>
      fetchJson<Record<string, unknown>>("/skills", {
        method: "POST",
        body: JSON.stringify({ path: value }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["skills"] });
      toast.success("导入完成");
    },
    onError: (error) => toast.error(error.message),
  });

  return (
    <div className="space-y-5">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">导入技能目录</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="skill-path">技能目录（留空即 skills/）</Label>
            <Input
              id="skill-path"
              value={path}
              placeholder="例如 skills"
              onChange={(event) => setPath(event.target.value)}
            />
          </div>
          <Button disabled={importDir.isPending} onClick={() => importDir.mutate(path.trim())}>
            导入
          </Button>
        </CardContent>
      </Card>

      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">已导入技能</CardTitle>
        </CardHeader>
        <CardContent>
          {list.isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
          {list.isError && <p className="text-sm text-coral">读不出来：{list.error?.message ?? ""}</p>}
          {list.data?.skills.length === 0 && (
            <p className="text-sm text-muted-foreground">还没有技能：先在上面导入一次。</p>
          )}
          {(list.data?.skills.length ?? 0) > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>slug</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>说明</TableHead>
                  <TableHead>状态</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {list.data?.skills.map((skill) => (
                  <TableRow key={skill.id}>
                    <TableCell className="font-mono text-xs">{skill.slug}</TableCell>
                    <TableCell>{skill.kind}</TableCell>
                    <TableCell>{skill.description}</TableCell>
                    <TableCell>
                      <Badge variant={skill.enabled ? "default" : "secondary"}>
                        {skill.enabled ? "已启用" : "已停用"}
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
