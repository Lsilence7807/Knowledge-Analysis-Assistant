# 文件：backend/app/services/ingest.py
# 作用：把上传的 CSV/XLSX 清洗后落成 DuckDB 数据集，并生成数据画像
# 阶段：P1 数据摄入
# 依赖：pandas、charset-normalizer、backend/app/core/db.py、backend/app/services/store.py
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pandas as pd

from app.core import config, db
from app.services import store

ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xls"}
CLEAN_RULES = {
    "normalize_columns": True,
    "infer_types": True,
    "drop_duplicates": True,
    "profile_missing": True,
}


class IngestError(ValueError):
    """上传文件无法成为有效数据集。"""


def ingest_file(path, name: str) -> dict:
    """读取并清洗文件，返回数据集标识、画像和清洗日志。"""
    source_path = Path(path)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise IngestError("只支持 CSV、XLSX 或 XLS 文件")
    if not source_path.exists() or source_path.stat().st_size == 0:
        raise IngestError("文件为空")
    if source_path.stat().st_size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise IngestError("文件超过上传大小限制")

    clean_log: list[str] = []
    try:
        frame = _read_frame(source_path, suffix, clean_log)
    except Exception as exc:
        raise IngestError(f"文件读取失败：{exc}") from exc
    if frame.empty or len(frame.columns) == 0:
        raise IngestError("文件没有可用数据")

    original_columns = list(frame.columns)
    frame.columns = _normalize_columns(frame.columns)
    if original_columns != list(frame.columns):
        clean_log.append("列名已归一化")

    before = len(frame)
    frame = frame.drop_duplicates().reset_index(drop=True)
    if len(frame) != before:
        clean_log.append(f"去重：删除 {before - len(frame)} 行")
    frame = _infer_types(frame, clean_log)

    dataset_id = f"d_{uuid.uuid4().hex[:8]}"
    table = db.register_table(dataset_id, frame)
    profile = _profile(frame)
    result = {
        "dataset_id": dataset_id,
        "table": table,
        "rows": len(frame),
        "cols": len(frame.columns),
        "profile": profile,
        "clean_log": clean_log,
        "table_version": 1,
    }
    store.insert_dataset(
        {
            "id": dataset_id,
            "name": name,
            "source_id": dataset_id,
            "source_path": str(source_path),
            "table_name": table,
            "rows": len(frame),
            "cols": len(frame.columns),
            "profile_json": json.dumps(profile, ensure_ascii=False),
            "clean_log": json.dumps(clean_log, ensure_ascii=False),
            "table_version": 1,
        }
    )
    return result


def _read_frame(path: Path, suffix: str, clean_log: list[str]) -> pd.DataFrame:
    if suffix == ".csv":
        raw = path.read_bytes()
        try:
            raw.decode("utf-8-sig")
            encoding = "utf-8-sig"
        except UnicodeDecodeError:
            encoding = "gb18030"
            raw.decode(encoding)
        frame = pd.read_csv(path, encoding=encoding)
        if encoding.lower() not in {"utf-8", "utf-8-sig"}:
            clean_log.append(f"检测到编码：{encoding}")
        return frame

    excel = pd.ExcelFile(path)
    if len(excel.sheet_names) > 1:
        clean_log.append(f"仅导入首个 sheet，忽略 {len(excel.sheet_names) - 1} 个 sheet")
    return pd.read_excel(excel, sheet_name=excel.sheet_names[0])


def _normalize_columns(columns) -> list[str]:
    normalized: list[str] = []
    used: dict[str, int] = {}
    for index, column in enumerate(columns, start=1):
        value = re.sub(r"\s+", "_", str(column).replace("\ufeff", "").strip().lower())
        value = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]", "_", value).strip("_") or f"column_{index}"
        used[value] = used.get(value, 0) + 1
        normalized.append(value if used[value] == 1 else f"{value}_{used[value]}")
    return normalized


def _infer_types(frame: pd.DataFrame, clean_log: list[str]) -> pd.DataFrame:
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        values = frame[column].astype("string").str.strip()
        numeric_values = pd.to_numeric(values.str.replace(r"[,$￥\s]", "", regex=True), errors="coerce")
        present = values.notna() & values.ne("")
        if present.any() and numeric_values[present].notna().mean() >= 0.8:
            numeric_present = numeric_values[present].dropna()
            if (numeric_present % 1 == 0).all():
                frame[column] = numeric_values.astype("Int64")
            else:
                frame[column] = numeric_values
            clean_log.append(f"类型推断：{column} 为数值")
            continue
        date_values = pd.to_datetime(values, errors="coerce", format="mixed")
        if present.any() and date_values[present].notna().mean() >= 0.8:
            frame[column] = date_values
            clean_log.append(f"类型推断：{column} 为日期")
        else:
            frame[column] = values
    return frame


def _profile(frame: pd.DataFrame) -> dict:
    columns = []
    for column in frame.columns:
        series = frame[column]
        columns.append(
            {
                "name": column,
                "dtype": str(series.dtype),
                "null_count": int(series.isna().sum()),
                "null_rate": round(float(series.isna().mean()), 6),
                "unique_count": int(series.nunique(dropna=True)),
            }
        )
    return {"rows": len(frame), "cols": len(frame.columns), "columns": columns}
