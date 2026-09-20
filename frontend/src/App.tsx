// 文件：frontend/src/App.tsx
// 作用：页面外壳——顶部导航栏 + 路由出口；路由树在 src/router.ts
// 阶段：F2a 前端工程
// 依赖：@tanstack/react-router、src/components/nav.tsx
import { Outlet } from "@tanstack/react-router";

import { Nav } from "@/components/nav";

export function Shell() {
  return (
    <div className="min-h-screen">
      <Nav />
      <main className="mx-auto max-w-[1080px] px-5 py-7">
        <Outlet />
      </main>
    </div>
  );
}
