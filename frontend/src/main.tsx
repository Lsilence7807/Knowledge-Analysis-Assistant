// 文件：frontend/src/main.tsx
// 作用：入口——挂 QueryClient（TanStack Query）、路由与 toast，并引入全局样式
// 阶段：F2a 前端工程
// 依赖：react、@tanstack/react-query、@tanstack/react-router、sonner、src/index.css
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { router } from "@/router";
import { Toaster } from "@/components/ui/sonner";
import "@/index.css";

// 本地工具类应用：失败就照实报错，不自动重试掩盖问题（后端 503 的开关提示要原样给用户看）
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster position="top-center" />
    </QueryClientProvider>
  </StrictMode>,
);
