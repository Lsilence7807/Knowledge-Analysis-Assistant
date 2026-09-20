// 文件：frontend/src/router.ts
// 作用：路由树与 router 实例（8 页 code-based 路由）；页面外壳在 App.tsx，两者分文件以免 fast refresh 失效
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-router、src/App.tsx、src/routes/*
import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";

import { Shell } from "@/App";
import { DatasetsPage } from "@/routes/datasets";
import { EvalsPage } from "@/routes/evals";
import { HomePage } from "@/routes/home";
import { JobsPage } from "@/routes/jobs";
import { KbPage } from "@/routes/kb";
import { McpPage } from "@/routes/mcp";
import { SettingsPage } from "@/routes/settings";
import { SkillsPage } from "@/routes/skills";

const rootRoute = createRootRoute({ component: Shell });
const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: HomePage });
const datasetsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/datasets", component: DatasetsPage });
const kbRoute = createRoute({ getParentRoute: () => rootRoute, path: "/kb", component: KbPage });
const skillsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/skills", component: SkillsPage });
const mcpRoute = createRoute({ getParentRoute: () => rootRoute, path: "/mcp", component: McpPage });
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsPage });
const evalsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/evals", component: EvalsPage });
const jobsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/jobs", component: JobsPage });

const routeTree = rootRoute.addChildren([
  indexRoute,
  datasetsRoute,
  kbRoute,
  skillsRoute,
  mcpRoute,
  settingsRoute,
  evalsRoute,
  jobsRoute,
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
