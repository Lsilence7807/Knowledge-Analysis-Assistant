// 文件：frontend/src/routes/home.tsx
// 作用：首页——导入数据 + 提问（聊天壳）+ 结论 + 图表；配置与插件类入口都在各自页面
// 阶段：F2a 前端工程
// 依赖：src/components/{upload,chat}.tsx、src/lib/dataset.ts
import { Chat } from "@/components/chat";
import { Upload } from "@/components/upload";
import { useDatasetId } from "@/lib/dataset";

export function HomePage() {
  const datasetId = useDatasetId();
  return (
    <div className="space-y-5">
      <Upload />
      {datasetId ? (
        <Chat />
      ) : (
        <p className="rounded-lg border-[3px] border-dashed border-ink bg-card px-4 py-6 text-sm text-muted-foreground">
          先在上面导入一份 CSV/XLSX（或在「数据集」页选一个已有数据集），再提问。
        </p>
      )}
    </div>
  );
}
