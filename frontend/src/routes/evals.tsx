// 文件：frontend/src/routes/evals.tsx
// 作用：评测页占位——P16 还没开工，后端没有端点；F2 只把骨架与路由先摆好
// 阶段：F2a 前端工程
// 依赖：src/components/ui/{card,badge}.tsx
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function EvalsPage() {
  return (
    <Card className="border-[3px] border-ink shadow-hard">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          评测
          <Badge variant="secondary">P16 未开工</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-muted-foreground">
        <p>用例集、基线对比与 faithfulness / context 指标由 P16 交付，后端当前没有端点可接。</p>
        <p>这一页先占位：路由与导航已就位，P16 落地后换成真实用例表与运行结果。</p>
      </CardContent>
    </Card>
  );
}
