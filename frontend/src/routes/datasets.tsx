// 文件：frontend/src/routes/datasets.tsx
// 作用：数据集页——列表、选中、画像、删除；首页只留导入与提问，管理动作都在这儿
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-query、src/lib/apiClient.ts、src/lib/dataset.ts、sonner
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchJson } from "@/lib/apiClient";
import { setDatasetId, useDatasetId } from "@/lib/dataset";

// ponytail: 这两个路由还没挂 response_model，类型先写在页面里；F1 挂上模型后换成生成的类型
type DatasetItem = {
  id: string;
  name: string;
  table_name: string;
  rows: number;
  cols: number;
  table_version: number;
  created_at: string;
};
type Profile = {
  dataset_id: string;
  table: string;
  rows: number;
  cols: number;
  profile: Record<string, unknown>;
  clean_log: string[];
  table_version: number;
};

export function DatasetsPage() {
  const selected = useDatasetId();
  const client = useQueryClient();
  const list = useQuery({ queryKey: ["datasets"], queryFn: () => fetchJson<DatasetItem[]>("/datasets") });
  const profile = useQuery({
    queryKey: ["dataset-profile", selected],
    queryFn: () => fetchJson<Profile>(`/datasets/${selected}/profile`),
    enabled: selected !== "",
  });
  const remove = useMutation({
    mutationFn: (id: string) => fetchJson<{ deleted: boolean }>(`/datasets/${id}`, { method: "DELETE" }),
    onSuccess: (_result, id) => {
      if (id === selected) setDatasetId("");
      void client.invalidateQueries({ queryKey: ["datasets"] });
      toast.success("数据集已删除");
    },
    onError: (error) => toast.error(error.message),
  });

  return (
    <div className="space-y-5">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">数据集</CardTitle>
        </CardHeader>
        <CardContent>
          {list.isPending && <p className="text-sm text-muted-foreground">加载中…</p>}
          {list.isError && <p className="text-sm text-coral">列表读不出来：{list.error?.message ?? ""}</p>}
          {list.data?.length === 0 && (
            <p className="text-sm text-muted-foreground">还没有数据集：回首页导入一份 CSV/XLSX。</p>
          )}
          {(list.data?.length ?? 0) > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>表</TableHead>
                  <TableHead>行</TableHead>
                  <TableHead>列</TableHead>
                  <TableHead>版本</TableHead>
                  <TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {list.data?.map((item) => (
                  <TableRow key={item.id} className={item.id === selected ? "bg-hair" : undefined}>
                    <TableCell>{item.name}</TableCell>
                    <TableCell className="font-mono text-xs">{item.table_name}</TableCell>
                    <TableCell>{item.rows}</TableCell>
                    <TableCell>{item.cols}</TableCell>
                    <TableCell>{item.table_version}</TableCell>
                    <TableCell className="space-x-2">
                      <Button
                        size="sm"
                        variant={item.id === selected ? "default" : "outline"}
                        onClick={() => setDatasetId(item.id)}
                      >
                        {item.id === selected ? "已选中" : "选中"}
                      </Button>
                      <Button size="sm" variant="destructive" onClick={() => remove.mutate(item.id)}>
                        删除
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {selected !== "" && (
        <Card className="border-[3px] border-ink shadow-hard">
          <CardHeader>
            <CardTitle className="text-base">画像：{selected}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {profile.isPending && <p className="text-muted-foreground">画像加载中…</p>}
            {profile.isError && <p className="text-coral">画像读不出来：{profile.error?.message ?? ""}</p>}
            {profile.data && (
              <>
                <p className="text-muted-foreground">
                  表 {profile.data.table} · {profile.data.rows} 行 × {profile.data.cols} 列 · 版本{" "}
                  {profile.data.table_version}
                </p>
                <pre className="max-h-[320px] overflow-auto rounded-md bg-hair px-3 py-2 text-xs">
                  {JSON.stringify(profile.data.profile, null, 2)}
                </pre>
                {profile.data.clean_log.length > 0 && (
                  <p className="text-xs text-muted-foreground">清洗日志：{profile.data.clean_log.join("；")}</p>
                )}
              </>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
