// 文件：frontend/src/router.ts
// 作用：路由树与 router 实例（9 页 code-based 路由，F7 加登录页）；外壳在 App.tsx，两者分文件以免 fast refresh 失效
// 阶段：F2a 前端工程；F7 加 /login（P11）
// 依赖：@tanstack/react-router、src/App.tsx、src/routes/*
import { createRootRoute, createRoute, createRouter, lazyRouteComponent } from "@tanstack/react-router";

import { Pending, Shell } from "@/App";

// 页面按路由懒加载：切到哪页才下哪页的包（图表页不再拖累其它页首屏）
const HomePage = lazyRouteComponent(() => import("@/routes/home"), "HomePage");
const DatasetsPage = lazyRouteComponent(() => import("@/routes/datasets"), "DatasetsPage");
const KbPage = lazyRouteComponent(() => import("@/routes/kb"), "KbPage");
const SkillsPage = lazyRouteComponent(() => import("@/routes/skills"), "SkillsPage");
const McpPage = lazyRouteComponent(() => import("@/routes/mcp"), "McpPage");
const SettingsPage = lazyRouteComponent(() => import("@/routes/settings"), "SettingsPage");
const EvalsPage = lazyRouteComponent(() => import("@/routes/evals"), "EvalsPage");
const JobsPage = lazyRouteComponent(() => import("@/routes/jobs"), "JobsPage");
const LoginPage = lazyRouteComponent(() => import("@/routes/login"), "LoginPage");

const rootRoute = createRootRoute({ component: Shell, pendingComponent: Pending });
const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: HomePage });
const datasetsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/datasets", component: DatasetsPage });
const kbRoute = createRoute({ getParentRoute: () => rootRoute, path: "/kb", component: KbPage });
const skillsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/skills", component: SkillsPage });
const mcpRoute = createRoute({ getParentRoute: () => rootRoute, path: "/mcp", component: McpPage });
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsPage });
const evalsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/evals", component: EvalsPage });
const jobsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/jobs", component: JobsPage });
const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login", component: LoginPage });

const routeTree = rootRoute.addChildren([
  indexRoute,
  datasetsRoute,
  kbRoute,
  skillsRoute,
  mcpRoute,
  settingsRoute,
  evalsRoute,
  jobsRoute,
  loginRoute,
]);

export const router = createRouter({ routeTree, defaultPendingComponent: Pending });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
