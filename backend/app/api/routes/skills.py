# 文件：backend/app/api/routes/skills.py
# 作用：技能端点：清单与导入（能力开关关闭时 503 并给开启方式）
# 阶段：F1 后端骨架（原 app/main.py 的 /skills 两个端点原样搬来）
# 依赖：fastapi、app/api/deps.py
from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_capability

router = APIRouter()


@router.get("/skills")
def skills_list(module=Depends(require_capability("skills"))) -> dict:
    """已导入技能清单；ENABLE_SKILLS 关闭时 503 并给开启方式，不猜也不返回空列表。"""
    return {"skills": module.list_skills()}


@router.post("/skills")
def skills_import(payload: dict = Body(default={}), module=Depends(require_capability("skills"))) -> dict:
    """导入技能目录（默认 skills/）：解析每个子目录的 SKILL.md 入库，坏的单独跳过。"""
    try:
        return module.import_dir(str(payload.get("path") or ""))
    except module.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
