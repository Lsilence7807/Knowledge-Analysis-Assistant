# 参与贡献

## 环境

与 README「怎么跑」一致：双击 `启动.cmd`，或手动装依赖后起 `uvicorn app.main:app --port 8017`。需要 Python 3.11+。

## 提交前必须全绿

CI 与下面三条同口径，push / PR 都会跑：

```
python -m ruff check app tests
python -m ruff format --check app tests
python -m pytest tests -q
```

测试必须在真实文件系统里跑（沙箱内 `tmp_path` 不可写，见 `docs/代码台账.md` K-017）。

## 约定

- **阶段制**：改动范围以 `docs/系统总体设计.md` §5 的阶段划分为准，越界先改设计文档再改代码。
- **一阶段一测试文件**：`tests/test_p*.py`，覆盖反例与降级路径；断言不满足就改代码，绝不为通过而改断言。
- **不顺手修无关 bug**：发现了记进 `docs/代码台账.md`「已知问题」（现象 / 触发条件 / 所在函数 / 是否修复）。
- **注释写「为什么」**：文件头用固定格式（见 `AGENTS.md`），不复述代码在做什么；禁止裸 TODO。
- **一次提交只包含一个阶段**，commit 格式：`P1 数据摄入：验收 test_p1_ingest 通过`。
- **不进仓库**：密钥、`data/`、数据库文件（`.gitignore` 已挡，提交前抽查一次）。
- **不 force push、不改写历史**。

## 目录

见 README 的「项目结构」一节。
