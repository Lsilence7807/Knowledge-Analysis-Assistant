// 文件：frontend/src/routes/jobs.tsx
// 作用：作业页占位——P18 还没开工，后端没有端点；F2 只把骨架与路由先摆好
// 阶段：F2a 前端工程
// 依赖：src/components/ui/{card,badge}.tsx
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function JobsPage() {
  return (
    <Card className="border-[3px] border-ink shadow-hard">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          作业
          <Badge variant="secondary">P18 未开工</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-muted-foreground">
        <p>定时同步、长任务与报告导出由 P18/P19 交付，后端当前没有端点可接。</p>
        <p>这一页先占位：路由与导航已就位，P18 落地后换成作业列表与运行日志。</p>
      </CardContent>
    </Card>
  );
}
