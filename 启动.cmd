@echo off
rem 文件：启动.cmd
rem 作用：双击即用：准备 Python 环境 -> 装锁定的依赖 -> 起服务 -> 自动打开页面（网页端交付）
rem 阶段：P5.2 一键启动
rem 依赖：Python 3.11+、requirements.lock
chcp 65001 >nul
cd /d "%~dp0"
set PORT=8017
set URL=http://127.0.0.1:%PORT%/

rem 端口被占时先探一下是不是本应用（旧进程占着端口时新进程会悄悄退出，这里挡掉）
netstat -ano | findstr /c:"LISTENING" | findstr /c:":%PORT% " >nul
if errorlevel 1 goto :setup
powershell -NoProfile -Command "try{if((Invoke-WebRequest -UseBasicParsing '%URL%health' -TimeoutSec 3).StatusCode -eq 200){exit 0}else{exit 2}}catch{exit 2}"
if errorlevel 1 goto :busy
echo 服务已经在跑，直接打开页面。
start "" %URL%
exit /b 0

:busy
echo 端口 %PORT% 被别的程序占着：关掉它再双击本文件，或者改用别的端口。
pause
exit /b 1

:setup
if exist ".venv\Scripts\python.exe" goto :run
echo 首次运行：准备 Python 环境并安装依赖（需要联网，几分钟，只做这一次）
py -3.12 -m venv .venv >nul 2>nul
if exist ".venv\Scripts\python.exe" goto :deps
py -3 -m venv .venv >nul 2>nul
if exist ".venv\Scripts\python.exe" goto :deps
python -m venv .venv >nul 2>nul
if exist ".venv\Scripts\python.exe" goto :deps
echo 没找到 Python 3.11 以上版本：装一个再双击本文件即可
echo https://www.python.org/downloads/windows/
pause
exit /b 1

:deps
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.lock
if errorlevel 1 (
  echo 锁文件装不上，改用 requirements.txt 装最新版
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
)

:frontend
if exist "web\dist\index.html" goto :run
where npm >nul 2>nul
if errorlevel 1 (
  echo 没找到 Node.js，跳过前端构建：页面会提示未构建，只调 API 不受影响。
  goto :run
)
echo 首次构建前端页面（需要联网，几分钟，只做这一次）
if exist "frontend\node_modules" (call npm --prefix frontend install) else (call npm --prefix frontend ci)
call npm --prefix frontend run build

:run
echo 正在启动，页面会自动打开；关掉这个窗口就停止服务。
echo %URL%
start "" %URL%
".venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --port %PORT%
pause
