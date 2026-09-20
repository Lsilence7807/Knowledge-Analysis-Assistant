# 文件：tests/test_p6_skills.py
# 作用：P6 验收测试：导入目录解析 frontmatter、列表、prompt 型取片段、script 型子进程执行；
#       反例：../../ 路径逃逸、脚本超时被杀、script 未声明入口、SKILL.md 缺 frontmatter、
#       禁用技能不可用、开关关闭时降级、白名单外的直调被拒（K-023 口径）
# 阶段：P6 Skill 导入与执行
# 依赖：contextlib、time、pytest、fastapi.testclient、app/config.py、app/main.py、
#       app/providers/skills.py、app/store.py、app/tools.py
from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, store, tools
from app.main import app
from app.providers import skills

PROMPT_SKILL = """---
name: demo_prompt
description: 演示用 prompt 型技能
kind: prompt
---

# 业务口径

金额单位是万元，比率保留一位小数。
"""

SCRIPT_SKILL = """---
name: demo_script
description: 演示用 script 型技能
kind: script
entry: run.py
---

需要跑脚本时用这个技能。
"""

SCRIPT_BODY = "import sys\nprint('技能脚本输出:' + ','.join(sys.argv[1:]))\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """数据目录与技能根目录都指向临时目录；技能能力显式打开（默认是关的）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    # 本机页面配过密钥时，不隔离这个路径就会拿真实密钥出网（同 K-019）
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(skills, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(skills, "ENABLED", True)
    with TestClient(app) as test_client:
        yield test_client


def _make_skill(root: Path, name: str, text: str, extra: dict | None = None) -> Path:
    """在临时技能根目录里造一个技能目录，返回它的路径。"""
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8", newline="")
    for filename, content in (extra or {}).items():
        (directory / filename).write_text(content, encoding="utf-8", newline="")
    return directory


def _capability_events(capability: str) -> list[str]:
    """读能力流水，供越权留痕断言用（同 test_p13 的读法）。"""
    with closing(store.connect()) as conn:
        rows = conn.execute("SELECT event FROM capability_log WHERE capability = ?", (capability,)).fetchall()
    return [row["event"] for row in rows]


def test_import_lists_skills_and_persists_rows(client, tmp_path):
    _make_skill(tmp_path, "demo_prompt", PROMPT_SKILL)
    _make_skill(tmp_path, "demo_script", SCRIPT_SKILL, {"run.py": SCRIPT_BODY})
    body = client.post("/skills", json={}).json()
    assert body["skipped"] == []
    assert [item["slug"] for item in body["imported"]] == ["demo_prompt", "demo_script"]
    assert "skills" in store.table_names()
    listed = client.get("/skills").json()["skills"]
    assert [row["slug"] for row in listed] == ["demo_prompt", "demo_script"]
    assert [row["kind"] for row in listed] == ["prompt", "script"]
    assert listed[0]["description"] == "演示用 prompt 型技能"
    assert [row["enabled"] for row in listed] == [1, 1]


def test_reimport_updates_instead_of_duplicating(client, tmp_path):
    _make_skill(tmp_path, "demo_prompt", PROMPT_SKILL)
    client.post("/skills", json={})
    client.post("/skills", json={})
    assert len(client.get("/skills").json()["skills"]) == 1


def test_prompt_skill_returns_body_without_frontmatter(client, tmp_path):
    _make_skill(tmp_path, "demo_prompt", PROMPT_SKILL)
    client.post("/skills", json={})
    result = skills.use_skill("demo_prompt")
    assert result["kind"] == "prompt"
    assert "金额单位是万元" in result["content"]
    assert "---" not in result["content"]


def test_long_prompt_body_is_capped(client, tmp_path):
    # 体检发现：正文原本无上限直通模型上下文（脚本输出却有），这里钉住同一个上限
    _make_skill(tmp_path, "huge", "---\nname: huge\nkind: prompt\n---\n\n" + "口径说明" * 5000)
    client.post("/skills", json={})
    result = skills.use_skill("huge")
    assert len(result["content"]) == skills.MAX_OUTPUT_CHARS
    assert result["truncated"] is True


def test_script_skill_runs_in_subprocess(client, tmp_path):
    _make_skill(tmp_path, "demo_script", SCRIPT_SKILL, {"run.py": SCRIPT_BODY})
    client.post("/skills", json={})
    result = skills.use_skill("demo_script", ["a", "b"])
    assert result["returncode"] == 0
    assert "技能脚本输出:a,b" in result["output"]


def test_import_path_escape_is_rejected(client, tmp_path):
    with pytest.raises(skills.SkillError):
        skills.import_dir("../../")
    assert client.post("/skills", json={"path": "../../"}).status_code == 400
    assert client.post("/skills", json={"path": "skills/../../.."}).status_code == 400


def test_skill_without_frontmatter_is_skipped(client, tmp_path):
    _make_skill(tmp_path, "demo_prompt", PROMPT_SKILL)
    _make_skill(tmp_path, "broken", "# 没有 frontmatter 的技能\n")
    body = client.post("/skills", json={}).json()
    assert [item["slug"] for item in body["imported"]] == ["demo_prompt"]
    assert len(body["skipped"]) == 1
    assert "frontmatter" in body["skipped"][0]["error"]


def test_script_without_entry_is_skipped(client, tmp_path):
    _make_skill(tmp_path, "no_entry", "---\nname: no_entry\nkind: script\n---\n\n没有声明入口\n")
    body = client.post("/skills", json={}).json()
    assert body["imported"] == []
    assert "entry" in body["skipped"][0]["error"]


def test_unknown_kind_is_skipped(client, tmp_path):
    _make_skill(tmp_path, "weird", "---\nname: weird\nkind: shell\n---\n\n类型不认识\n")
    assert "kind" in client.post("/skills", json={}).json()["skipped"][0]["error"]


def test_script_timeout_is_killed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "TIMEOUT_S", 1)
    _make_skill(
        tmp_path,
        "slow",
        "---\nname: slow\nkind: script\nentry: run.py\n---\n\n慢脚本\n",
        {"run.py": "import time\ntime.sleep(30)\n"},
    )
    client.post("/skills", json={})
    started = time.monotonic()
    with pytest.raises(skills.SkillError) as excinfo:
        skills.use_skill("slow")
    assert "超时" in str(excinfo.value)
    assert time.monotonic() - started < 15


def test_disabled_skill_is_listed_but_refused(client, tmp_path):
    _make_skill(tmp_path, "off", "---\nname: off\nkind: prompt\nenabled: false\n---\n\n不该出现\n")
    client.post("/skills", json={})
    assert client.get("/skills").json()["skills"][0]["enabled"] == 0
    with pytest.raises(skills.SkillError) as excinfo:
        skills.use_skill("off")
    assert "禁用" in str(excinfo.value)


def test_skill_tools_work_when_whitelisted(client, tmp_path, monkeypatch):
    _make_skill(tmp_path, "demo_prompt", PROMPT_SKILL)
    client.post("/skills", json={})
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["list_skills", "use_skill"]})
    assert tools.call("list_skills", {"dataset_id": "d_test"})["skills"][0]["slug"] == "demo_prompt"
    assert "万元" in tools.call("use_skill", {"slug": "demo_prompt"})["content"]


def test_skill_tools_are_refused_outside_whitelist(client, tmp_path, monkeypatch):
    _make_skill(tmp_path, "demo_prompt", PROMPT_SKILL)
    client.post("/skills", json={})
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_sql"]})
    with pytest.raises(tools.ToolError):
        tools.call("use_skill", {"slug": "demo_prompt"})
    assert "error" in _capability_events("tools")


def test_capability_reports_available_when_enabled(client):
    assert client.get("/capabilities").json()["capabilities"]["skills"] is True


def test_disabled_switch_degrades_without_error(client, monkeypatch):
    monkeypatch.setattr(skills, "ENABLED", False)
    assert client.get("/capabilities").json()["capabilities"]["skills"] is False
    assert client.get("/skills").status_code == 503
    # 工具层同样只降级：给模型一句可读的拒绝，而不是抛 500
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["list_skills"]})
    with pytest.raises(tools.ToolError) as excinfo:
        tools.call("list_skills", {})
    assert "技能能力未启用" in str(excinfo.value)
