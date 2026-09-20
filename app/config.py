# 文件：app/config.py
# 作用：集中读取环境变量、本地设置（config/local.json）与路径，其它模块只从这里取配置
# 阶段：P0 骨架与契约冻结（P3 加密钥来源，P5 加密钥写入）
# 依赖：标准库 json、os、pathlib
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
SQLITE_PATH = DATA_DIR / "meta.sqlite"
DUCKDB_PATH = DATA_DIR / "analytics.duckdb"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "6"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))
ENABLE_AGENT = os.getenv("ENABLE_AGENT", "true").lower() == "true"

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
MODELS_CONFIG = BASE_DIR / "config" / "models.json"
TOOLS_CONFIG = BASE_DIR / "config" / "tools.json"
LOCAL_SETTINGS = BASE_DIR / "config" / "local.json"


def ensure_dirs() -> None:
    """创建数据目录，幂等。"""
    (Path(DATA_DIR) / "files").mkdir(parents=True, exist_ok=True)


def local_settings() -> dict:
    """读页面写入的本地设置；文件不在或读坏了一律当没有，不抛错。"""
    try:
        return json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def api_key() -> str:
    """取模型密钥：本地文件优先，环境变量兜底；每次调用都读，页面保存后立即生效。"""
    return str(local_settings().get("llm_api_key") or "") or LLM_API_KEY
