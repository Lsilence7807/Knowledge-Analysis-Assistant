# 文件：tests/test_ui_browser.py
# 作用：真浏览器端到端（Playwright + Chromium + 真服务 + 真模型）：登录门 → 8 个路由零控制台报错
#       → 上传 → 提问（SSE 流式）→ 结果表 → 数据集列表与删除 → 知识库导入/检索 → MCP 调用
# 阶段：F 段之后的浏览器补测（2026-09-22 起）；没有 UI 入口的部分见 docs/留白-未测项.md
# 依赖：pytest、playwright（可选，没装就整模块跳过）、httpx、backend/app/core/security.py（算口令哈希）
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api", reason="浏览器验收要装 playwright 与 Chromium（见 docs/留白-未测项.md）")

from playwright.sync_api import expect, sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("KAA_E2E_PORT", "8018"))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "e2e-pass-123"
CSV_BODY = "地区,销售额\n华东,10\n华南,20\n华北,30\n"
QUESTION = "各地区的销售额合计是多少"
LOGIN_SUBMIT = "button[type=submit]"
PAGES = ("/", "/datasets", "/kb", "/skills", "/mcp", "/settings", "/evals", "/jobs")

# 真机口径：要真模型密钥（config/local.json）、要构建过前端、要装好 Chromium，因此默认整模块跳过
pytestmark = pytest.mark.skipif(
    os.environ.get("KAA_E2E") != "1",
    reason=(
        "真机浏览器验收：设 KAA_E2E=1 才跑（$env:KAA_E2E='1'; "
        "& .\\.venv\\Scripts\\python.exe -m pytest tests/test_ui_browser.py -q）"
    ),
)


class Tab:
    """页面包装：收集控制台报错 / 未捕获异常 / 5xx 响应（页面白屏与后端炸掉的两大来源）。

    Playwright 的 Page 不让随便挂属性，所以包一层，其余调用原样透传。
    """

    def __init__(self, page) -> None:
        self._page = page
        self.errors: list[str] = []
        self.server_errors: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.errors.append(f"pageerror: {error}"))
        page.on("response", self._response)

    def __getattr__(self, name):
        return getattr(self._page, name)

    def _console(self, message) -> None:
        if message.type == "error":
            self.errors.append(f"console.error: {message.text}")

    def _response(self, response) -> None:
        if response.status >= 500:
            self.server_errors.append(f"{response.status} {response.url}")

    def console_errors(self) -> list[str]:
        """控制台报错：滤掉 Failed to load resource——那是状态码带出的噪音，5xx 另有 server_errors 兜住。"""
        return [item for item in self.errors if "Failed to load resource" not in item]

    def assert_clean(self, where: str = "") -> None:
        """页面要干净：没有未捕获异常、没有非资源类控制台报错、没有 5xx。"""
        assert self.console_errors() == [], f"{where} 控制台报错：{self.console_errors()}"
        assert self.server_errors == [], f"{where} 服务端 5xx：{self.server_errors}"


def _serve(data_dir: Path) -> dict:
    """真服务环境：全能力打开 + 开认证 + 数据目录隔离（模型密钥仍读 config/local.json）。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core import security

    flags = (
        "ENABLE_AGENT",
        "ENABLE_MEMORY",
        "ENABLE_CACHE",
        "ENABLE_SEMANTIC_CACHE",
        "ENABLE_KB",
        "ENABLE_SKILLS",
        "ENABLE_SANDBOX",
        "ENABLE_MCP",
        "ENABLE_STREAM",
    )
    return {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "ENABLE_AUTH": "true",
        "AUTH_DISABLED": "false",
        "APP_PASSWORD_HASH": security.hash_password(PASSWORD),
        "ENABLE_JOBS": "true",
        **{name: "true" for name in flags},
    }


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """起真服务（uvicorn + app.main），跑完关掉；端口默认 8018，不碰 启动.cmd 的 8017。"""
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip("前端还没构建：先 npm --prefix frontend run build")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            "backend",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=_serve(tmp_path_factory.mktemp("e2e-data")),
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                pytest.fail(f"服务没起来（退出码 {proc.returncode}）")
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"60 秒内 {BASE}/health 没起来")
            time.sleep(1)
        yield BASE
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def engine(server):
    """整模块一个 Chromium 进程。"""
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="module")
def context(engine):
    """已登录上下文：cookie 直接走接口登录拿（UI 登录由 test_login_* 单独验，两条路互不干扰）。"""
    ctx = engine.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
    response = httpx.post(f"{BASE}/login", json={"password": PASSWORD}, timeout=15)
    assert response.status_code == 200, response.text
    ctx.add_cookies([{"name": "kaa_session", "value": response.cookies["kaa_session"], "url": BASE}])
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture()
def page(context):
    tab = Tab(context.new_page())
    try:
        yield tab
    finally:
        tab._page.close()


@pytest.fixture()
def anon_page(engine):
    """干净上下文（没 cookie）：登录门用例专用。"""
    ctx = engine.new_context(viewport={"width": 1280, "height": 900})
    try:
        yield Tab(ctx.new_page())
    finally:
        ctx.close()


def _login(tab, password: str = PASSWORD) -> None:
    """从登录页正常登录：口令对了会跳回主页。"""
    tab.goto(f"{BASE}/login")
    tab.fill("#password", password)
    tab.click(LOGIN_SUBMIT)


def test_login_is_required_and_wrong_password_is_reported(anon_page):
    """没登录时调用接口的页面会被 401 拉回登录页；口令错当场报错，对了才回主页。"""
    anon_page.goto(f"{BASE}/datasets")  # 这条路由挂载时就会拉接口，401 才会被拉回登录页
    anon_page.wait_for_url("**/login")
    anon_page.fill("#password", "definitely-wrong")
    anon_page.click(LOGIN_SUBMIT)
    expect(anon_page.locator("p.text-destructive")).to_be_visible()
    anon_page.assert_clean("登录页")
    _login(anon_page)
    anon_page.wait_for_url(f"{BASE}/")
    expect(anon_page.locator("header nav")).to_be_visible()


def test_every_page_renders_without_console_errors(page):
    """8 个路由逐个「直接粘链接进来」：页面渲染、主区域有内容、没有控制台报错与 5xx。

    /datasets 与 /skills 跟接口同路径，靠 main.py 的 spa_over_api 按 Accept 分流——这条就是它的回归。
    """
    for path in PAGES:
        page.goto(BASE + path)
        expect(page.locator("header nav")).to_be_visible()
        page.wait_for_function(
            "() => (document.querySelector('main')?.innerText ?? '').trim().length > 4",
            timeout=30_000,
        )
        page.assert_clean(path)


def test_upload_then_ask_streams_a_result_table(page, tmp_path):
    """上传 CSV → 主页出现对话 → 提问走 SSE 流式 → 结果表渲染出行（真模型，给足超时）。"""
    csv = tmp_path / "e2e-sales.csv"
    csv.write_text(CSV_BODY, encoding="utf-8")
    page.goto(f"{BASE}/")
    page.set_input_files("input[type=file]", str(csv))
    composer = page.locator("textarea").first  # assistant-ui 另有一个 aria-hidden 的隐藏 textarea
    expect(composer).to_be_visible(timeout=60_000)  # 选中数据集后主页才渲染对话
    with page.expect_response(lambda response: "/ask/stream" in response.url, timeout=180_000) as seen:
        composer.fill(QUESTION)
        page.get_by_role("button", name="提问").click()
    assert seen.value.status == 200
    assert "text/event-stream" in seen.value.headers.get("content-type", "")
    tabs = page.get_by_role("tab")
    expect(tabs.nth(1)).to_be_visible(timeout=180_000)
    tabs.nth(1).click()
    expect(page.locator("table tbody tr").first).to_be_visible()
    assert page.locator("table tbody tr").count() >= 3, "结果表没有数据行"
    page.assert_clean("提问")


def test_datasets_page_lists_profiles_and_deletes(page):
    """数据集页：列表里有刚上传的那份，点行出画像，删除后少一行。"""
    page.goto(f"{BASE}/datasets")
    rows = page.locator("table tbody tr")
    expect(rows.first).to_be_visible(timeout=30_000)
    page.locator("table tbody tr").first.click()
    expect(page.locator("pre").first).to_be_visible(timeout=30_000)
    before = rows.count()
    page.get_by_role("button", name="删除").first.click()
    expect(rows).to_have_count(before - 1, timeout=30_000)
    page.assert_clean("数据集页")


def test_kb_import_then_search_hits_a_chunk(page):
    """知识库页：导入仓库内一份技能文档，再按关键词检索到片段。"""
    page.goto(f"{BASE}/kb")
    page.fill("#kb-path", "skills/demo_skill")
    page.get_by_role("button", name="导入").click()
    expect(page.locator("pre").first).to_contain_text("imported", timeout=60_000)
    page.fill("#kb-query", "caveats")
    page.get_by_role("button", name="检索").click()
    expect(page.locator("main")).to_contain_text("片", timeout=30_000)
    assert "没有命中" not in page.locator("main").inner_text()
    page.assert_clean("知识库页")


def test_mcp_page_lists_tools_and_calls_echo(page):
    """MCP 页：列出工具（真起 stdio server）→ 选中 echo 调一次，回显进 pre。"""
    page.goto(f"{BASE}/mcp")
    rows = page.locator("table tbody tr")
    expect(rows.first).to_be_visible(timeout=60_000)
    rows.filter(has_text="echo").first.get_by_role("button").click()
    expect(page.locator("#mcp-tool")).to_have_value("echo")
    page.fill("#mcp-args", '{"text": "hello-e2e"}')
    page.get_by_role("button", name="调用").click()
    expect(page.locator("pre").first).to_contain_text("hello-e2e", timeout=60_000)
    page.assert_clean("MCP 页")


def test_jobs_page_pins_the_missing_ui(page):
    """P18 的 /jobs 与 P19 的 /exports 接口 F9 已落地，但前端作业页还是 F2 占位——这里钉住现状。

    真做这套 UI 时删掉本用例，换成「作业列表 + 导出按钮」的验收。
    """
    page.goto(f"{BASE}/jobs")
    expect(page.locator("main")).to_contain_text("未开工")
