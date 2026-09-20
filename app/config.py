# 文件：app/config.py
# 作用：集中读取环境变量、本地设置（config/local.json）与路径，其它模块只从这里取配置
# 阶段：P0 骨架与契约冻结（P3 加密钥来源，P5 加密钥写入；P21 加并发与压测路径）
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
EXEC_MAX_WORKERS = int(os.getenv("EXEC_MAX_WORKERS", "4"))

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
MODELS_CONFIG = BASE_DIR / "config" / "models.json"
TOOLS_CONFIG = BASE_DIR / "config" / "tools.json"
LOCAL_SETTINGS = BASE_DIR / "config" / "local.json"
BENCH_BASELINE = BASE_DIR / "bench" / "baseline.json"


def ensure_dirs() -> None:
    """创建数据目录，幂等。"""
    (Path(DATA_DIR) / "files").mkdir(parents=True, exist_ok=True)


def local_settings() -> dict:
    """读页面写入的本地设置；文件不在或读坏了一律当没有，不抛错。"""
    try:
        return json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(**patch) -> dict:
    """合并写 config/local.json：字典按键合并、「带 id 的列表」按 id 合并，其余整体覆盖；返回写后的内容。"""
    current = local_settings()
    for key, value in patch.items():
        old = current.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            current[key] = {**old, **value}
        elif isinstance(value, list) and isinstance(old, list):
            merged = {str(item.get("id")): item for item in old if isinstance(item, dict)}
            for item in value:
                if isinstance(item, dict):
                    merged[str(item.get("id"))] = {**merged.get(str(item.get("id")), {}), **item}
            current[key] = list(merged.values())
        else:
            current[key] = value
    LOCAL_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_SETTINGS.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def api_key(profile: dict | None = None) -> str:
    """取某个模型 profile 的密钥：本地文件（页面写入）优先 → 该 profile 声明的环境变量 → 通用 LLM_API_KEY。

    每次调用都读文件，所以页面保存后立即生效，不用重启。
    """
    profile = profile or {}
    keys = local_settings().get("api_keys") or {}
    from_file = keys.get(str(profile.get("id") or ""))
    env_name = str(profile.get("api_key_env") or "")
    from_env = os.getenv(env_name) if env_name else ""
    return str(from_file or "") or from_env or LLM_API_KEY
