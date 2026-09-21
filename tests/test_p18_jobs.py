# 文件：tests/test_p18_jobs.py
# 作用：F9/P18 验收：作业提交与进度（/jobs）、长作业真进队列并由消费者跑完、huey 不可用时同步退化、
#       重复提交去重、重启标 interrupted、定时清理、作业失败原因可读、能力开关关闭回 503
# 阶段：F9 作业与文档（兼 P18）
# 依赖：time、pytest、fastapi.testclient、app/core/config.py、app/main.py、
#       app/schemas/__init__.py、app/services/{ingest,insight,jobs,nlu,store}.py
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import create_app
from app.schemas import Insight
from app.services import ingest, insight, jobs, nlu, store

QUESTION = "各地区的销售额合计是多少"
SAMPLE = "examples/demo_sales.csv"
BIG_ROWS = 500_000


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """数据目录与作业库都指向临时目录；作业能力显式打开，模型出口由用例替换。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "HUEY_DB", tmp_path / "jobs.db")
    monkeypatch.setattr(config, "ENABLE_JOBS", True)
    monkeypatch.setattr(config, "ENABLE_AGENT", False)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    return ingest.ingest_file(config.BASE_DIR / SAMPLE, "demo_sales.csv")


@pytest.fixture()
def client(env):
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def off_client(tmp_path, monkeypatch):
    """作业能力关掉的客户端：用于验 503 与提示文案。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "HUEY_DB", tmp_path / "jobs.db")
    monkeypatch.setattr(config, "ENABLE_JOBS", False)
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def fake_model(monkeypatch):
    """确定性替身：单跳 SQL 固定，结论不含结果表里没有的数字。"""

    def fake_to_sql(question, profile, context):
        return f"SELECT 地区, sum(销售额) AS 销售额合计 FROM {profile['table']} GROUP BY 地区"

    monkeypatch.setattr(nlu, "to_sql", fake_to_sql)
    monkeypatch.setattr(insight, "summarize", lambda *args, **kwargs: _insight("按地区汇总完成，明细见结果表"))
    return fake_to_sql


def _insight(summary: str) -> Insight:
    return Insight(summary=summary, findings=[], anomalies=[], suggestions=[], confidence="medium", caveats=[])


def _submit(client, dataset_id, rows=None):
    payload = {"question": QUESTION, "dataset_id": dataset_id}
    if rows is not None:
        payload["rows"] = rows
    return client.post("/jobs", json={"kind": "ask", "payload": payload})


def _wait_done(client, job_id, timeout=10.0):
    """等消费者把作业跑完：队列是异步的，测试按状态轮询而不是睡固定时间。"""
    deadline = time.time() + timeout
    body = client.get(f"/jobs/{job_id}").json()
    while body["status"] not in ("done", "failed") and time.time() < deadline:
        time.sleep(0.1)
        body = client.get(f"/jobs/{job_id}").json()
    return body


# ---- 提交、进度与结果 ----


def test_short_job_runs_inline_with_readable_result(client, env, fake_model):
    """自动：小数据集当场跑完（不必进队列），结果里有 SQL、列名与行数。"""
    response = _submit(client, env["dataset_id"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"].startswith("j_") and body["status"] == "done"
    result = body["result"]
    assert result["sql"].lower().startswith("select")
    assert result["columns"] == ["地区", "销售额合计"] and result["row_count"] > 0
    assert body["progress"] == 1.0
    assert client.get(f"/jobs/{body['job_id']}").json()["result"]["row_count"] == result["row_count"]


def test_big_job_goes_to_queue_and_consumer_finishes_it(client, env, monkeypatch):
    """自动：预判超 10s 的作业进 huey 队列，消费者线程把它跑到 done（不是当着请求跑）。"""

    def slow_to_sql(question, profile, context):
        time.sleep(0.6)  # 给「提交返回时还在队列里」留出可观察的窗口
        return f"SELECT 地区, count(*) AS 行数 FROM {profile['table']} GROUP BY 地区"

    monkeypatch.setattr(nlu, "to_sql", slow_to_sql)
    monkeypatch.setattr(insight, "summarize", lambda *args, **kwargs: _insight("按地区计数完成"))
    assert jobs.worker_alive() is True  # 生命周期里起的消费者
    body = _submit(client, env["dataset_id"], rows=BIG_ROWS).json()
    assert body["status"] in ("queued", "running")
    done = _wait_done(client, body["job_id"])
    assert done["status"] == "done", done
    assert done["result"]["row_count"] > 0


def test_estimate_prefers_background_for_big_datasets(env):
    """自动：预判只看行数与是否走 agent——大表超阈值、小表不超。"""
    assert jobs.estimate_seconds({"rows": BIG_ROWS}) > jobs.BACKGROUND_AFTER_S
    assert jobs.estimate_seconds({"rows": 240}) < jobs.BACKGROUND_AFTER_S


def test_duplicate_submit_returns_same_job(client, env, fake_model):
    """反例：同一入参已有执行中的作业时不再开一个，回同一个 job_id。"""
    store.insert_job(
        {
            "id": "j_running",
            "kind": "ask",
            "payload": {"question": QUESTION, "dataset_id": env["dataset_id"], "rows": env["rows"]},
            "status": "running",
        }
    )
    body = _submit(client, env["dataset_id"]).json()
    assert body["job_id"] == "j_running"
    assert len(store.list_jobs(50)) == 1


def test_missing_job_is_404(client):
    """反例：不存在的 job_id 给 404，不回空壳。"""
    response = client.get("/jobs/j_nope")
    assert response.status_code == 404 and "作业不存在" in response.json()["detail"]


def test_failed_job_records_readable_error(client, env, monkeypatch):
    """反例：作业内部报错落 failed + 原因，接口不回 500。"""

    def boom(*args, **kwargs):
        raise RuntimeError("模型网关没配")

    monkeypatch.setattr(nlu, "to_sql", boom)
    body = _submit(client, env["dataset_id"]).json()
    assert body["status"] == "failed" and "模型网关没配" in body["error"]


def test_startup_marks_unfinished_jobs_interrupted(env):
    """反例：重启把排队/执行中的作业标 interrupted；已经跑完的不动。"""
    store.insert_job({"id": "j_q", "kind": "ask", "payload": {}, "status": "queued"})
    store.insert_job({"id": "j_done", "kind": "ask", "payload": {}, "status": "done"})
    assert store.mark_interrupted() == 1
    assert store.get_job("j_q")["status"] == "interrupted"
    assert store.get_job("j_q")["error"]
    assert store.get_job("j_done")["status"] == "done"


def test_queue_unavailable_falls_back_to_sync(client, env, fake_model, monkeypatch):
    """反例：huey 写不进去时当着请求跑完，作业不许卡在 queued。"""

    def broken_queue():
        raise RuntimeError("jobs.db 只读")

    monkeypatch.setattr(jobs, "_queue", broken_queue)
    body = _submit(client, env["dataset_id"], rows=BIG_ROWS).json()
    assert body["status"] == "done" and body["result"]["row_count"] > 0


def test_capability_off_returns_503_with_hint(off_client):
    """反例：ENABLE_JOBS=false 时 /jobs 回 503，并把开启方式写在 detail 里。"""
    response = off_client.post("/jobs", json={"kind": "ask", "payload": {"question": QUESTION}})
    assert response.status_code == 503 and "ENABLE_JOBS" in response.json()["detail"]


def test_bad_kind_is_400(client, env):
    """反例：不支持的作业类型给 400，而不是悄悄当 ask 跑。"""
    response = client.post("/jobs", json={"kind": "train", "payload": {"question": QUESTION}})
    assert response.status_code == 400


def test_prune_keeps_active_jobs_and_drops_old_finished_ones(env):
    """自动：定时清理只删旧的终态作业，排队中的留着。"""
    store.insert_job({"id": "j_old", "kind": "ask", "payload": {}, "status": "done"})
    store.insert_job({"id": "j_new", "kind": "ask", "payload": {}, "status": "queued"})
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET created_at = '2020-01-01 00:00:00' WHERE id = 'j_old'")
        conn.commit()
    assert jobs.prune(7) == 1
    assert store.get_job("j_old") is None and store.get_job("j_new") is not None
