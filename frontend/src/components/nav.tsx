// 文件：frontend/src/components/nav.tsx
// 作用：顶部导航栏——8 页切换；首页只留导入/提问/结论/图表，配置与插件类各归其页（2026-09-21 定的信息架构）
// 阶段：F2a 前端工程
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

export function Nav() {
  return (
    <header className="sticky top-0 z-10 border-b-[3px] border-ink bg-card">
      <nav className="mx-auto flex max-w-[1080px] flex-wrap items-center gap-1 px-5 py-3">
        <Link to="/" className="mr-3 text-lg font-extrabold tracking-wide">
          AI 数据分析助手
        </Link>
        {PAGES.map((page) => (
          <Link
            key={page.to}
            to={page.to}
            className="rounded-full px-3 py-1 text-sm font-semibold text-ink-soft hover:bg-hair"
            activeProps={{ className: "rounded-full px-3 py-1 text-sm font-semibold bg-violet text-primary-foreground" }}
            activeOptions={{ exact: true }}
          >
            {page.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
