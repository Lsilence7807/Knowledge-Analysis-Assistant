// 文件：frontend/src/routes/home.tsx
// 作用：首页——hero（大标题 + 插画 + 装饰）+ 导入数据 + 提问（聊天壳）+ 结论 + 图表
// 阶段：F2a 前端工程；P5.3 视觉风格 v2 加 hero 与装饰层
// 依赖：src/components/{upload,chat,decor}.tsx、src/lib/dataset.ts
import { Chat } from "@/components/chat";
import { Blob, Eyebrow, Illustration, TiltNote } from "@/components/decor";
import { Upload } from "@/components/upload";
import { useDatasetId } from "@/lib/dataset";

export function HomePage() {
  const datasetId = useDatasetId();
  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-center gap-x-10 gap-y-8">
        <div className="min-w-[280px] flex-1 space-y-3">
          <Eyebrow>本地数据分析 · 中文提问</Eyebrow>
          <h1 className="text-4xl leading-[1.05] font-black tracking-tight sm:text-5xl">
            少写 SQL，
            <span className="block text-violet">多拿结论。</span>
          </h1>
          <p className="max-w-[44ch] text-sm text-muted-foreground">
            导入一份 CSV / XLSX，用中文问一句：模型自己写 SQL、跑沙箱、给结论与图表。数据不出本机。
          </p>
        </div>
        <div className="relative mx-auto shrink-0">
          <Blob tone="mint" className="-top-6 -left-8 size-24" />
          <Blob tone="yellow" className="-right-4 -bottom-6 size-16" />
          <Illustration name="data-board" alt="数据看板插画：便签、清单与结论卡片" className="relative w-[330px] max-w-full" />
          <TiltNote title="三步" tilt="right" className="absolute -bottom-8 -left-8">
            导入 → 提问 → 结论
          </TiltNote>
        </div>
      </section>

      <Upload />

      {datasetId ? (
        <Chat />
      ) : (
        <div className="flex flex-wrap items-center gap-5 rounded-2xl border-[3px] border-dashed border-ink bg-card px-5 py-5 shadow-hard-sm">
          <Illustration name="chart-buddy" alt="柱状图小助手插画" className="w-[104px]" />
          <p className="min-w-[220px] flex-1 text-sm text-muted-foreground">
            先在上面导入一份 CSV/XLSX（或在「数据集」页选一个已有数据集），再提问。
          </p>
        </div>
      )}
    </div>
  );
}
