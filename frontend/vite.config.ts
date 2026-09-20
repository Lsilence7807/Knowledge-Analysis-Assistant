// 文件：frontend/vite.config.ts
// 作用：Vite 配置——React + Tailwind v4 插件、@ 别名、构建产物落 web/dist、开发期把后端 API 前缀代理到本地后端
// 阶段：F2a 前端工程
// 依赖：vite、@vitejs/plugin-react、@tailwindcss/vite
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发期 Vite 只服务前端，API 必须转给后端；生产由 FastAPI 同源托管 web/dist，前端一律用相对路径
const API_PREFIXES = [
  '/health', '/capabilities', '/datasets', '/query', '/stats', '/tools', '/bench',
  '/skills', '/kb', '/ask', '/insight', '/settings', '/sandbox', '/metrics', '/mcp',
  '/openapi.json', '/docs',
]
// 后端端口按 docs/并行开发.md 分工表分配；要连别的窗口的后端时用环境变量覆盖
const apiTarget = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8017'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': '/src' } },
  build: { outDir: '../web/dist', emptyOutDir: true },
  server: {
    proxy: Object.fromEntries(
      API_PREFIXES.map((prefix) => [prefix, { target: apiTarget, changeOrigin: true }]),
    ),
  },
})
