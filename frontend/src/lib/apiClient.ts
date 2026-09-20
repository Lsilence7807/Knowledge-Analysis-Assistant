// 文件：frontend/src/lib/apiClient.ts
// 作用：后端 API 客户端：类型来自 /openapi.json 生成的 api-types.ts；另附一个 JSON 小工具给还没挂响应模型的路由
// 阶段：F2a 前端工程
// 依赖：openapi-fetch、src/lib/api-types.ts（改契约后跑 npm run gen:api 重新生成）
import createClient from "openapi-fetch";

import type { paths } from "@/lib/api-types";

// 同源相对路径：生产由 FastAPI 托管 web/dist，开发期由 vite.config.ts 的 proxy 转给后端
export const api = createClient<paths>();

// ponytail: 路由还没挂 response_model 时 /openapi.json 里没有响应结构，页面用显式泛型兜住；
// F1 给路由挂上响应模型后，这些泛型应换成 api-types.ts 里生成的类型，本函数随之删掉
export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new Error(errorDetail(body) ?? `HTTP ${res.status}`);
  return body as T;
}

function errorDetail(body: unknown): string | undefined {
  if (body !== null && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  return undefined;
}
