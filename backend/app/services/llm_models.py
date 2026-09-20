# 文件：backend/app/services/llm_models.py
# 作用：模型 profile 的读取与解析：models.json 打底，config/local.json 里的页面配置覆盖
# 阶段：P3 自然语言转 SQL
# 依赖：json、backend/app/core/config.py
from __future__ import annotations

import json

from app.core import config


class ModelNotFound(KeyError):
    """模型配置缺失，或请求的 profile 不存在。"""


def _config() -> dict:
    """读 config/models.json；文件缺失或格式坏了都算配置不可用，由调用方降级。"""
    try:
        return json.loads(config.MODELS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelNotFound(f"模型配置不可用：{exc}") from exc


def list_models() -> list[dict]:
    """返回全部 profile：models.json 打底，config/local.json 里的同 id 覆盖（页面新增的也在其中）。"""
    merged: dict[str, dict] = {}
    for model in _builtin_models():
        merged[str(model.get("id"))] = dict(model)
    for model in config.local_settings().get("models") or []:
        merged[str(model.get("id"))] = dict(model)
    return list(merged.values())


def resolve(model_id: str | None = None) -> dict:
    """按 id 取 profile，id 为空时取 default；找不到抛 ModelNotFound。"""
    target = model_id or config.local_settings().get("default") or _default_id()
    for model in list_models():
        if model.get("id") == target:
            return model
    raise ModelNotFound(f"没有这个模型 profile：{target}")


def _builtin_models() -> list[dict]:
    """models.json 里的 profile；文件缺失或坏了就当没有（页面配置仍可用）。"""
    try:
        return list(_config().get("models") or [])
    except ModelNotFound:
        return []


def _default_id() -> str | None:
    try:
        return _config().get("default")
    except ModelNotFound:
        return None
