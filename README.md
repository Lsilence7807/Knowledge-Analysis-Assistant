# AI 数据分析助手

数据接入 查询与统计 提问与多步分析 结论输出

上传一张业务表格，用中文提问，模型自己规划几步、依次调用查询与统计工具，最后给出结果表、结论和建议，网页上直接看。

## 怎么跑

```
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 页面：http://127.0.0.1:8000/ ；接口文档：/docs
# 模型：在页面上填任意 OpenAI 兼容厂商的 base_url + 模型名 + 密钥（写本机 config/local.json，不进仓库）
```

质量门与验收（MVP 七段 + 台账补丁，快照 74 passed）：

```
python -m ruff check app tests
python -m ruff format --check app tests
python -m pytest tests -q
```

## 文档

- `docs/索引.md` —— 文档入口与阅读顺序
- `docs/最小可用集-功能说明.md` —— 这一版能做什么、不能做什么、怎么跑
- `docs/代码台账.md` —— 已完成文件、已知问题、待推送记录
- `docs/交接-*.md` —— 最新的会话交接（换会话先读它）
