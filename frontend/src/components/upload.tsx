// 文件：frontend/src/components/upload.tsx
// 作用：导入数据——上传 CSV/XLSX 落成数据集，成功后顺手选中它，首页就能直接提问
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-query、src/lib/dataset.ts、sonner
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { setDatasetId, useDatasetId } from "@/lib/dataset";

type IngestResult = { dataset_id: string; table: string; rows: number; cols: number };

// 上传走原生 fetch：multipart 由浏览器拼边界，走生成的客户端反而要手写序列化
export function Upload() {
  const client = useQueryClient();
  const datasetId = useDatasetId();
  const [busy, setBusy] = useState(false);

  async function send(file: File): Promise<void> {
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/datasets", { method: "POST", body: form });
      const body = (await res.json().catch(() => null)) as ({ detail?: string } & Partial<IngestResult>) | null;
      if (!res.ok) throw new Error(body?.detail ?? `导入失败：HTTP ${res.status}`);
      if (!body?.dataset_id) throw new Error("导入成功但没拿到数据集 id");
      setDatasetId(body.dataset_id);
      void client.invalidateQueries({ queryKey: ["datasets"] });
      toast.success(`已导入 ${file.name}：${body.rows ?? 0} 行 × ${body.cols ?? 0} 列`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="border-[3px] border-ink shadow-hard">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-3 text-base">
          导入数据
          <span className="text-xs font-normal text-muted-foreground">
            {datasetId ? `当前数据集：${datasetId}` : "还没选数据集"}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <input
          type="file"
          accept=".csv,.xlsx,.xls"
          disabled={busy}
          className="text-sm"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void send(file);
          }}
        />
        <p className="mt-2 text-xs text-muted-foreground">支持 CSV / XLSX / XLS；导入后会做列名归一化、去重与类型推断。</p>
      </CardContent>
    </Card>
  );
}
