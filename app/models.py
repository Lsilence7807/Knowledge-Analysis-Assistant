# 文件：app/models.py
# 作用：模型 profile 的读取与解析（MVP 只有一个 DeepSeek profile）
# 阶段：P3 自然语言转 SQL
# 依赖：json、app/config.py
from __future__ import annotations

import json

from app import config


class ModelNotFound(KeyError):
    """模型配置缺失，或请求的 profile 不存在。"""


def _config() -> dict:
    """读 config/models.json；文件缺失或格式坏了都算配置不可用，由调用方降级。"""
    try:
        return json.loads(config.MODELS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelNotFound(f"模型配置不可用：{exc}") from exc


def list_models() -> list[dict]:
    """返回全部模型 profile。"""
    return list(_config().get("models") or [])


def resolve(model_id: str | None = None) -> dict:
    """按 id 取 profile，id 为空时取 default；找不到抛 ModelNotFound。"""
    data = _config()
    target = model_id or data.get("default")
    for model in data.get("models") or []:
        if model.get("id") == target:
            return dict(model)
    raise ModelNotFound(f"没有这个模型 profile：{target}")
