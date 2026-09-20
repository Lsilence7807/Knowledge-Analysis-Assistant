# 文件：tests/test_p1_ingest.py
# 作用：P1 数据摄入验收测试：清洗、编码、Excel、拒绝条件与画像路由；A 类补丁加删除数据集与上传失败清理（K-015）
# 阶段：P1 数据摄入
# 依赖：pytest、pandas、fastapi.testclient、app.ingest
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    with TestClient(app) as test_client:
        yield test_client


def test_dirty_csv_is_cleaned_and_profiled(client):
    with (FIXTURES / "dirty.csv").open("rb") as handle:
        response = client.post("/datasets", files={"file": ("dirty.csv", handle, "text/csv")})
    assert response.status_code == 200
    body = response.json()
    assert body["table"].startswith("ds_")
    assert body["rows"] == 3
    assert body["table_version"] == 1
    assert [column["name"] for column in body["profile"]["columns"]] == ["name", "amount", "date", "note", "empty"]
    assert any(column["name"] == "amount" and "int" in column["dtype"].lower() for column in body["profile"]["columns"])
    profile = client.get(f"/datasets/{body['dataset_id']}/profile")
    assert profile.status_code == 200
    assert profile.json()["rows"] == 3


def test_gbk_csv_is_read(client, tmp_path):
    source = tmp_path / "gbk.csv"
    source.write_bytes("名称,金额\n甲,12\n".encode("gbk"))
    with source.open("rb") as handle:
        response = client.post("/datasets", files={"file": ("gbk.csv", handle, "text/csv")})
    assert response.status_code == 200
    assert response.json()["profile"]["columns"][0]["name"] == "名称"


def test_xlsx_uses_first_sheet_and_logs_ignored_sheet(client, tmp_path):
    source = tmp_path / "sales.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"Product": ["A"]}).to_excel(writer, index=False, sheet_name="sales")
        pd.DataFrame({"Ignored": ["B"]}).to_excel(writer, index=False, sheet_name="ignored")
    with source.open("rb") as handle:
        response = client.post("/datasets", files={"file": ("sales.xlsx", handle)})
    assert response.status_code == 200
    assert "忽略" in "".join(response.json()["clean_log"])


def test_upload_rejections_are_readable_and_do_not_create_dataset(client, monkeypatch):
    assert client.post("/datasets", files={"file": ("bad.exe", b"x")}).status_code == 400
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    assert client.post("/datasets", files={"file": ("large.csv", b"a\n1\n")}).status_code == 413
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 50)
    assert client.post("/datasets", files={"file": ("empty.csv", b"")}).status_code == 400
    assert client.get("/datasets").json() == []


def test_broken_xlsx_returns_readable_error(client):
    response = client.post("/datasets", files={"file": ("broken.xlsx", b"not an xlsx")})
    assert response.status_code == 400
    assert response.json()["detail"]


def test_missing_dataset_profile_is_404(client):
    response = client.get("/datasets/d_missing/profile")
    assert response.status_code == 404


def test_delete_dataset_removes_table_metadata_and_source_file(client, tmp_path):
    """K-015：删数据集要把表、元数据、上传文件三份都收走；再删一次是 404。"""
    with (FIXTURES / "dirty.csv").open("rb") as handle:
        body = client.post("/datasets", files={"file": ("dirty.csv", handle, "text/csv")}).json()
    dataset_id = body["dataset_id"]
    assert len(list((tmp_path / "files").glob("upload_*"))) == 1

    response = client.delete(f"/datasets/{dataset_id}")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "dataset_id": dataset_id,
        "table": body["table"],
        "deleted": True,
        "removed_source": True,
    }
    assert client.get("/datasets").json() == []
    assert client.get(f"/datasets/{dataset_id}/profile").status_code == 404
    assert list((tmp_path / "files").glob("upload_*")) == []
    with db.connect(read_only=True) as conn:
        tables = [row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()]
    assert body["table"] not in tables
    assert client.delete(f"/datasets/{dataset_id}").status_code == 404


def test_failed_ingest_leaves_no_upload_file(client, tmp_path):
    """K-015：清洗失败的上传文件要当场删掉，否则 data/files 只增不减。"""
    assert client.post("/datasets", files={"file": ("broken.xlsx", b"not an xlsx")}).status_code == 400
    assert list((tmp_path / "files").glob("upload_*")) == []
