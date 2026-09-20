# 文件：backend/app/api/routes/datasets.py
# 作用：数据集端点：上传、列表、画像、删除
# 阶段：F1 后端骨架（原 app/main.py 的 4 个 /datasets* 端点原样搬来）
# 依赖：fastapi、app/api/deps.py、app/services/{db,ingest,store}.py
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import get_dataset, get_settings
from app.core import db
from app.services import ingest, store

router = APIRouter()


@router.post("/datasets")
async def upload_dataset(file: UploadFile = File(...), settings=Depends(get_settings)) -> dict:
    """接收文件并返回清洗后的数据集画像。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ingest.ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="只支持 CSV、XLSX 或 XLS 文件")
    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")
    settings.ensure_dirs()
    target = settings.DATA_DIR / "files" / f"upload_{uuid4().hex}{suffix}"
    target.write_bytes(content)
    try:
        return ingest.ingest_file(target, file.filename or target.name)
    except ingest.IngestError as exc:
        # 清洗失败就没有数据集指向这个文件，留着只会变孤儿（K-015）
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/datasets")
def datasets() -> list[dict]:
    """返回已上传数据集列表。"""
    return store.list_datasets()


@router.get("/datasets/{dataset_id}/profile")
def dataset_profile(dataset: dict = Depends(get_dataset)) -> dict:
    """返回指定数据集的画像与清洗日志；不存在由 get_dataset 给 404。"""
    return {
        "dataset_id": dataset["id"],
        "table": dataset["table_name"],
        "rows": dataset["rows"],
        "cols": dataset["cols"],
        "profile": dataset["profile"],
        "clean_log": dataset["clean_log"],
        "table_version": dataset["table_version"],
    }


@router.delete("/datasets/{dataset_id}")
def drop_dataset(dataset: dict = Depends(get_dataset)) -> dict:
    """删除数据集：DuckDB 表、原始上传文件与元数据一起清（K-015 最小版）。"""
    source = Path(str(dataset.get("source_path") or ""))
    removed_source = source.is_file()
    if removed_source:
        source.unlink(missing_ok=True)
    db.drop_table(dataset["id"])
    store.delete_dataset(dataset["id"])
    return {
        "dataset_id": dataset["id"],
        "table": dataset["table_name"],
        "deleted": True,
        "removed_source": removed_source,
    }
