// 文件：frontend/src/components/nav.tsx
// 作用：顶部导航栏——8 页切换；首页只留导入/提问/结论/图表，配置与插件类各归其页（2026-09-21 定的信息架构）
// 阶段：F2a 前端工程；P5.3 视觉风格 v2 加品牌标记与药丸态
// 依赖：@tanstack/react-router
import { Link } from "@tanstack/react-router";

const PAGES = [
  { to: "/", label: "首页" },
  { to: "/datasets", label: "数据集" },
  { to: "/kb", label: "知识库" },
  { to: "/skills", label: "技能" },
  { to: "/mcp", label: "MCP" },
  { to: "/settings", label: "设置" },
  { to: "/evals", label: "评测" },
  { to: "/jobs", label: "作业" },
] as const;

const LINK = "rounded-full border-[3px] border-transparent px-3 py-1 text-sm font-semibold text-ink-soft";

export function Nav() {
  return (
    <header className="sticky top-0 z-10 border-b-[3px] border-ink bg-card">
      <nav className="mx-auto flex max-w-[1080px] flex-wrap items-center gap-1 px-5 py-3">
        <Link to="/" className="mr-3 flex items-center gap-2">
          <span className="grid size-9 rotate-[-4deg] place-items-center rounded-xl border-[3px] border-ink bg-violet text-sm font-black text-primary-foreground shadow-hard-sm">
            AI
          </span>
          <span className="text-lg font-extrabold tracking-tight">AI 数据分析助手</span>
        </Link>
        {PAGES.map((page) => (
          <Link
            key={page.to}
            to={page.to}
            className={LINK + " hover:border-ink hover:bg-yellow"}
            activeProps={{ className: LINK + " border-ink bg-violet text-primary-foreground shadow-hard-sm" }}
            activeOptions={{ exact: true }}
          >
            {page.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
