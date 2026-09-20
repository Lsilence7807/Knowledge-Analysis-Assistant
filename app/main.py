# 文件：app/main.py
# 作用：HTTP 路由与编排，唯一装配点；禁止在此出现 pandas 调用与 SQL 字符串
# 阶段：P0 骨架与契约冻结（P1 加数据集路由，P2 加查询路由）
# 依赖：FastAPI、app/config.py、app/registry.py、app/schemas.py、app/store.py
from __future__ import annotations

from contextlib import asynccontextmanager

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile

from app import config, ingest, registry, store
from app.schemas import CapabilitiesOut, HealthOut


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时准备数据目录与元数据表；失败即启动失败，不带病运行。"""
    config.ensure_dirs()
    store.ensure_tables()
    yield


app = FastAPI(title="Knowledge Analysis Assistant", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """存活检查。"""
    return HealthOut()


@app.get("/capabilities", response_model=CapabilitiesOut)
def capabilities() -> CapabilitiesOut:
    """各能力是否可用；不可用由调用方降级，不返回 5xx。"""
    return CapabilitiesOut(
        capabilities=registry.status(),
        descriptions=dict(registry.CAPABILITIES),
    )


@app.post("/datasets")
async def upload_dataset(file: UploadFile = File(...)) -> dict:
    """接收文件并返回清洗后的数据集画像。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ingest.ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="只支持 CSV、XLSX 或 XLS 文件")
    content = await file.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")
    config.ensure_dirs()
    target = config.DATA_DIR / "files" / f"upload_{uuid4().hex}{suffix}"
    target.write_bytes(content)
    try:
        return ingest.ingest_file(target, file.filename or target.name)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/datasets")
def datasets() -> list[dict]:
    """返回已上传数据集列表。"""
    return store.list_datasets()


@app.get("/datasets/{dataset_id}/profile")
def dataset_profile(dataset_id: str) -> dict:
    """返回指定数据集的画像与清洗日志。"""
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {
        "dataset_id": dataset["id"],
        "table": dataset["table_name"],
        "rows": dataset["rows"],
        "cols": dataset["cols"],
        "profile": dataset["profile"],
        "clean_log": dataset["clean_log"],
        "table_version": dataset["table_version"],
    }
