# 文件：tests/test_p2_sql.py
# 作用：P2 查询与统计层验收测试：SQL 守卫、只读、超时、行数上限、描述统计与异常
# 阶段：P2 查询与统计层
# 依赖：pytest、pandas、fastapi.testclient、app.db、app.store
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core import config, db
from app.main import app
from app.services import store

SAMPLE = pd.DataFrame(
    {
        "region": ["华东", "华南", "华北"] * 7,
        "amount": [10, 11, 12] * 7,
        "note": ["a", None, "c"] * 7,
    }
)

# 每条都必须被拒，且表不能被改动
BAD_SQL = [
    "DROP TABLE ds_d_test",
    "CREATE TABLE t AS SELECT 1",
    "DELETE FROM ds_d_test",
    "UPDATE ds_d_test SET amount = 0",
    "INSERT INTO ds_d_test VALUES ('华东', 1, 'x')",
    "SELECT 1; DROP TABLE ds_d_test",
    "SELECT 1 --; DROP TABLE ds_d_test",
    "SELECT 1 /* ; DROP TABLE ds_d_test */",
    "SELECT 1;",
    "WITH x AS (DELETE FROM ds_d_test) SELECT 1",
    "",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    with TestClient(app) as test_client:
        yield test_client


def _make_dataset(frame: pd.DataFrame, dataset_id: str = "d_test") -> str:
    """登记一个数据集（DuckDB 表 + sqlite 元数据），返回表名。"""
    store.ensure_tables()
    table = db.register_table(dataset_id, frame)
    store.insert_dataset(
        {
            "id": dataset_id,
            "name": "t.csv",
            "table_name": table,
            "rows": len(frame),
            "cols": len(frame.columns),
            "profile_json": "{}",
            "clean_log": "[]",
            "table_version": 1,
        }
    )
    return table


def test_select_returns_columns_and_rows(client):
    _make_dataset(SAMPLE)
    response = client.post(
        "/query",
        json={"sql": "SELECT region, sum(amount) AS total FROM ds_d_test GROUP BY 1 ORDER BY 2 DESC"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["columns"] == ["region", "total"]
    assert body["row_count"] == 3
    assert body["rows"] == [["华北", 84], ["华南", 77], ["华东", 70]]
    assert body["truncated"] is False


@pytest.mark.parametrize("sql", BAD_SQL)
def test_bad_sql_is_rejected_with_readable_detail(client, sql):
    _make_dataset(SAMPLE)
    response = client.post("/query", json={"sql": sql})
    assert response.status_code == 400
    assert response.json()["detail"]


def test_rejected_sql_leaves_data_untouched(client):
    _make_dataset(SAMPLE)
    for sql in BAD_SQL:
        client.post("/query", json={"sql": sql})
    body = client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"}).json()
    assert body["rows"] == [[21]]


def test_literals_may_contain_comment_and_semicolon_chars(client):
    """K-002：字面量与带引号标识符里的 --、/*、; 是数据，不该触发守卫。"""
    _make_dataset(SAMPLE)
    body = client.post(
        "/query",
        json={"sql": "SELECT 'a;b' AS s, '--x' AS c, '/*y*/' AS z, 'it''s;--x' AS q, 1 AS \"k;--\""},
    ).json()
    assert body["columns"] == ["s", "c", "z", "q", "k;--"]
    assert body["rows"] == [["a;b", "--x", "/*y*/", "it's;--x", 1]]


def test_literal_does_not_smuggle_second_statement(client):
    """字面量放行不等于放开分号：代码里带第二条语句照样拒，表也不受影响。"""
    _make_dataset(SAMPLE)
    assert client.post("/query", json={"sql": "SELECT 'a;b'; DROP TABLE ds_d_test"}).status_code == 400
    assert client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"}).json()["rows"] == [[21]]


def test_missing_table_returns_readable_400_not_500(client):
    response = client.post("/query", json={"sql": "SELECT * FROM ds_missing"})
    assert response.status_code == 400
    assert "ds_missing" in response.json()["detail"]


def test_bad_request_body_is_422_not_400(client):
    """K-013：请求体走 pydantic，缺字段或类型错是 422，不再落成 400 或静默默认值。"""
    _make_dataset(SAMPLE)
    assert client.post("/query", json={"sql": 123}).status_code == 422
    assert client.post("/query", json={}).status_code == 422
    assert client.post("/stats", json={"dataset_id": "d_test", "columns": "region"}).status_code == 422


def test_result_over_limit_is_truncated_with_hint(client):
    _make_dataset(pd.DataFrame({"i": range(6000)}), "d_big")
    body = client.post("/query", json={"sql": "SELECT i FROM ds_d_big"}).json()
    assert body["row_count"] == 5000
    assert len(body["rows"]) == 5000
    assert body["truncated"] is True
    assert "5000" in body["hint"]


def test_timeout_interrupts_and_service_survives(client):
    _make_dataset(SAMPLE)
    slow = (
        "WITH RECURSIVE t AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM t WHERE i < 1000000000) SELECT count(*) FROM t"
    )
    with pytest.raises(db.SQLRejected) as rejected:
        db.exec_sql(slow, timeout_s=1)
    assert "已中断" in str(rejected.value)
    assert client.get("/health").status_code == 200
    body = client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"}).json()
    assert body["rows"] == [[21]]


def test_stats_returns_description(client):
    _make_dataset(SAMPLE)
    response = client.post("/stats", json={"dataset_id": "d_test"})
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 21
    amount = next(item for item in body["columns"] if item["name"] == "amount")
    assert amount["count"] == 21
    assert amount["mean"] == pytest.approx(11)
    assert amount["median"] == pytest.approx(11)
    assert (amount["min"], amount["max"]) == (10, 12)
    note = next(item for item in body["columns"] if item["name"] == "note")
    assert note["missing"] == 7
    assert "mean" not in note
    assert body["anomalies"] == []


def test_stats_flags_outlier_with_reason(client):
    _make_dataset(pd.DataFrame({"amount": [10, 11, 12] * 7 + [1000]}), "d_out")
    body = client.post("/stats", json={"dataset_id": "d_out"}).json()
    hit = next(item for item in body["anomalies"] if item["column"] == "amount")
    assert hit["value"] == 1000
    assert "高于 IQR 上界" in hit["reason"]
    assert "第 22 行" == hit["row_hint"]


def test_stats_missing_dataset_is_404(client):
    assert client.post("/stats", json={"dataset_id": "d_nope"}).status_code == 404
