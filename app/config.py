# 文件：app/config.py
# 作用：集中读取环境变量与路径，其它模块只从这里取配置
# 阶段：P0 骨架与契约冻结
# 依赖：标准库 os、pathlib
from __future__ import annotations

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


def ensure_dirs() -> None:
    """创建数据目录，幂等。"""
    (Path(DATA_DIR) / "files").mkdir(parents=True, exist_ok=True)
