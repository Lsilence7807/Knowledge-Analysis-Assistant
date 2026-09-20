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


def availability() -> list[dict]:
    """每个 profile 的可用性：没配密钥的标 unavailable（P9 的口径，页面与能力表都看它）。"""
    return [
        {
            "id": model.get("id"),
            "status": "available" if config.api_key(model) else "unavailable",
            "purpose": list(model.get("purpose") or []),
        }
        for model in list_models()
    ]


def deployments() -> list[dict]:
    """profile → LiteLLM deployment：profile id 与它的每个 purpose 各当一个模型组。

    没配密钥的 profile 不进表（标 unavailable），免得每次调用都白试一遍再等超时。
    """
    out: list[dict] = []
    for model in list_models():
        key = config.api_key(model)
        if not key:
            continue
        params: dict = {"model": litellm_model(model), "api_key": key, "timeout": model.get("timeout_s", 30)}
        if model.get("base_url"):
            params["api_base"] = model["base_url"]
        profile_id = str(model.get("id"))
        out.append({"model_name": profile_id, "litellm_params": dict(params)})
        for purpose in model.get("purpose") or []:
            out.append({"model_name": str(purpose), "litellm_params": dict(params)})
    return out


def fallbacks() -> list[dict]:
    """models.json 的 `fallback` 链 → LiteLLM 的 fallbacks 参数。

    支持两种写法：顶层 `"fallback": ["second"]`（兜所有组）与 profile 上的 `"fallback": ["second"]`（只兜该 profile）。
    """
    try:
        cfg = _config()
    except ModelNotFound:
        return []
    chains: dict[str, list[str]] = {}
    for name in cfg.get("fallback") or []:
        for model in list_models():
            chains.setdefault(str(model.get("id")), []).append(str(name))
    for model in list_models():
        chain = [str(x) for x in (model.get("fallback") or [])]
        if chain:
            chains[str(model.get("id"))] = chain + chains.get(str(model.get("id")), [])
    return [{key: value} for key, value in chains.items() if value]


def fallback_names(model: dict) -> list[str]:
    """该 profile 的回退链（按名字给 LiteLLM 的单次调用用）。"""
    try:
        cfg = _config()
    except ModelNotFound:
        cfg = {}
    return [str(x) for x in ((model.get("fallback") or []) or (cfg.get("fallback") or []))]


def purpose_groups() -> dict[str, list[str]]:
    """purpose → 声明了它的 profile id 列表（页面与测试用它看分组）。"""
    groups: dict[str, list[str]] = {}
    for model in list_models():
        for purpose in model.get("purpose") or []:
            groups.setdefault(str(purpose), []).append(str(model.get("id")))
    return groups


def litellm_model(model: dict) -> str:
    """profile → LiteLLM 的 model 串：默认按 OpenAI 兼容端点走，写了厂商前缀的原样透传。"""
    name = str(model.get("model"))
    if "/" in name and not model.get("base_url"):
        return name
    prefix = "anthropic" if model.get("provider") == "anthropic" else "openai"
    return f"{prefix}/{name}"
