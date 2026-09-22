@echo off
rem 文件：启动-需登录.cmd
rem 作用：带单密码认证启动（公网 / 共享机器用）：从 config/auth.local.json 读口令串，只给本进程树设环境变量，不写系统环境
rem 阶段：P11 公开端（本地启动脚本，2026-09-23 加）
rem 依赖：Python 3.11+、requirements.lock、config/auth.local.json（本机密文件，.gitignore 挡住）
chcp 65001 >nul
cd /d "%~dp0"
set PORT=8017
set URL=http://127.0.0.1:%PORT%/

if not exist ".venv\Scripts\python.exe" (
  echo 还没有 Python 环境：先双击「启动.cmd」跑一次把环境建起来。
  pause
  exit /b 1
)
if not exist "config\auth.local.json" (
  echo 缺 config\auth.local.json：里面要放 {"APP_PASSWORD_HASH": "pbkdf2_sha256$..."}；
  echo 生成命令见 docs\问题总表.md 的「换机器 / 换 agent 必读」。
  pause
  exit /b 1
)

echo 正在启动（需要登录），页面会自动打开；关掉这个窗口就停止服务。
echo %URL%
powershell -NoProfile -Command "$a = Get-Content 'config\auth.local.json' -Raw | ConvertFrom-Json; $env:ENABLE_AUTH = 'true'; $env:APP_PASSWORD_HASH = $a.APP_PASSWORD_HASH; Start-Process '%URL%'; & '.venv\Scripts\python.exe' -m uvicorn app.main:app --app-dir backend --port %PORT%"
pause
