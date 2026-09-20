# 文件：tests/test_p21_perf.py
# 作用：P21 验收——统一阻塞执行器、50 万行聚合 p95、事件循环心跳 p95、10 并发无 5xx、/bench 退化行为
# 阶段：P21 性能与并发契约
# 依赖：pytest、httpx、asyncio、duckdb、app/{config,db,exec,main}
from __future__ import annotations

import asyncio
import json
import threading
import time

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app import exec as exec_pool
from app.main import app

ROWS = 500_000
SQL = "SELECT bucket, COUNT(*) AS n, AVG(amount) AS amount FROM perf GROUP BY bucket ORDER BY bucket"
BUDGET_S = 1.5  # §4.14：单表 50 万行聚合 p95 ≤ 1.5s
HEARTBEAT_P95_S = 0.2  # §4.14：事件循环心跳延迟 p95 ≤ 200ms


@pytest.fixture()
def perf_env(tmp_path, monkeypatch):
    """隔离数据目录并造 50 万行表，返回临时目录。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "BENCH_BASELINE", tmp_path / "baseline.json")
    with duckdb.connect(str(config.DUCKDB_PATH)) as conn:
        conn.execute(
            f"CREATE TABLE perf AS SELECT (range % 1000) AS bucket, (range % 97) * 1.5 AS amount FROM range({ROWS})"
        )
    return tmp_path


def p95(samples: list[float]) -> float:
    """升序样本的第 95 百分位；样本很少时退化成最大值。"""
    ordered = sorted(samples)
    return ordered[max(0, round(len(ordered) * 0.95) - 1)]


def test_worker_pool_size_defaults_to_four():
    """§4.14：默认 EXEC_MAX_WORKERS=4。"""
    assert config.EXEC_MAX_WORKERS == 4


def test_run_blocking_runs_off_caller_thread():
    """阻塞函数必须落到执行器线程，而不是调用方线程。"""
    assert exec_pool.run_blocking(threading.get_ident) != threading.get_ident()


def test_exec_sql_goes_through_runner(perf_env, monkeypatch):
    """db.exec_sql 内部必须经 run_blocking 调度（P21 的唯一入口约定）。"""
    seen = []
    real = db.run_blocking

    def spy(fn, /, *args, **kwargs):
        seen.append(getattr(fn, "__name__", "?"))
        return real(fn, *args, **kwargs)

    monkeypatch.setattr(db, "run_blocking", spy)
    db.exec_sql(SQL)
    assert seen == ["_run_select"]


def test_500k_aggregate_within_budget(perf_env):
    """50 万行聚合单次必须落在 §4.14 的 1.5s 预算内。"""
    start = time.perf_counter()
    result = db.exec_sql(SQL)
    elapsed = time.perf_counter() - start
    assert result["row_count"] == 1000
    assert elapsed <= BUDGET_S, f"50 万行聚合 {elapsed:.3f}s 超过预算 {BUDGET_S}s"


async def _run_concurrent(batch: int = 10, rounds: int = 3):
    """并发打 /query，同时用 10ms 心跳采样事件循环延迟。"""
    stop = asyncio.Event()
    samples: list[float] = []

    async def heartbeat():
        while not stop.is_set():
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            samples.append(max(0.0, time.perf_counter() - start - 0.01))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://perf") as client:
        beat = asyncio.create_task(heartbeat())
        responses = []
        for _ in range(rounds):
            responses += await asyncio.gather(*[client.post("/query", json={"sql": SQL}) for _ in range(batch)])
        stop.set()
        await beat
    return responses, samples


def test_heartbeat_p95_and_10_concurrent_queries(perf_env):
    """10 并发查询期间事件循环心跳 p95 ≤ 200ms，且全部 200（§4.14）。"""
    responses, samples = asyncio.run(_run_concurrent())
    assert [response.status_code for response in responses] == [200] * len(responses)
    assert all(response.json()["row_count"] == 1000 for response in responses)
    assert len(samples) >= 20, f"心跳样本太少：{len(samples)}"
    beat_p95 = p95(samples)
    assert beat_p95 <= HEARTBEAT_P95_S, f"心跳 p95 {beat_p95:.3f}s 超过 {HEARTBEAT_P95_S}s"


def test_bench_endpoint_degrades_then_reads_baseline(perf_env):
    """/bench：没跑过压测时给结构化说明，跑过就回基线内容。"""
    with TestClient(app) as client:
        empty = client.get("/bench")
        assert empty.status_code == 200
        assert empty.json()["available"] is False
        assert "bench.py" in empty.json()["hint"]

        config.BENCH_BASELINE.write_text(json.dumps({"p95_s": 0.4}), encoding="utf-8")
        filled = client.get("/bench")
        assert filled.status_code == 200
        assert filled.json() == {"available": True, "baseline": {"p95_s": 0.4}}
