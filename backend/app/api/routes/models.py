# 文件：backend/app/api/routes/models.py
# 作用：/settings/models 端点：列模型 profile、保存 profile 与密钥（保存即生效，不回传密钥明文）
# 阶段：F1 后端骨架（原 app/main.py 的 /settings/models 两个端点原样搬来）
# 依赖：fastapi、app/api/deps.py、app/services/llm_models.py
from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import get_settings
from app.services import llm_models

router = APIRouter()


@router.get("/settings/models")
def list_model_settings(settings=Depends(get_settings)) -> dict:
    """可用模型 profile：密钥只回「是否已配」，不回明文。"""
    return {"models": [_public_model(model, settings) for model in llm_models.list_models()]}


@router.put("/settings/models")
def save_model_settings(payload: dict = Body(...), settings=Depends(get_settings)) -> dict:
    """新增或更新一个 OpenAI 兼容 profile、写密钥到 config/local.json，并设为默认；保存即生效。"""
    profile_id = str(payload.get("id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    model_name = str(payload.get("model") or "").strip()
    if not (profile_id and base_url and model_name):
        raise HTTPException(status_code=400, detail="id、base_url、model 都是必填")
    profile = {
        "id": profile_id,
        "provider": str(payload.get("provider") or "openai_compatible"),
        "base_url": base_url,
        "model": model_name,
        "purpose": payload.get("purpose") or ["sql", "insight"],
    }
    if payload.get("api_key_env"):
        profile["api_key_env"] = str(payload["api_key_env"])
    patch = {"models": [profile], "default": profile_id}
    if "api_key" in payload:
        # 传了 api_key 才写盘（空串等于清掉，好让页面能演示「没密钥只出表格」）
        patch["api_keys"] = {profile_id: str(payload.get("api_key") or "")}
    settings.save_settings(**patch)
    return list_model_settings(settings)


def _public_model(model: dict, settings) -> dict:
    """对外只给 id/厂商/模型名与是否已配密钥，密钥本体不出网关。"""
    return {
        "id": model.get("id"),
        "provider": model.get("provider"),
        "base_url": model.get("base_url"),
        "model": model.get("model"),
        "has_key": bool(settings.api_key(model)),
    }
