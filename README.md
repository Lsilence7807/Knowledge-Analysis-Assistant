# AI 数据分析助手

数据接入 查询与统计 提问与多步分析 结论输出

上传一张业务表格，用中文提问，模型自己规划几步、依次调用查询与统计工具，最后给出结果表、结论和建议，网页上直接看。

## 怎么跑

```
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 复现当前环境锁版本：pip install -r requirements.lock（requirements.txt 留给想装最新版本的人）
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8017
# 起服务前先确认端口没被旧进程占着：Get-NetTCPConnection -LocalPort 8017 -State Listen
# 页面：http://127.0.0.1:8017/ ；接口文档：/docs
# 模型：在页面上填任意 OpenAI 兼容厂商的 base_url + 模型名 + 密钥（写本机 config/local.json，不进仓库）
```

质量门与验收（MVP 七段 + 台账补丁，快照 79 passed；测试必须在真实文件系统里跑，沙箱内 `tmp_path` 不可写，见 `docs/代码台账.md` 的 K-017）：

```
python -m ruff check app tests
python -m ruff format --check app tests
python -m pytest tests -q
```

## 文档

- `docs/索引.md` —— 文档入口与阅读顺序
- `docs/最小可用集.md` —— 第一版（MVP）做了什么、怎么跑、扩展契约
- `docs/代码台账.md` —— 已完成文件、已知问题、待推送记录
- `docs/交接-*.md` —— 最新的会话交接（换会话先读它）
