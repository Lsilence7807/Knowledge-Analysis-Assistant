# 文件：app/schemas.py
# 作用：HTTP 边界的 pydantic 模型，接口契约以本文件为准
# 阶段：P0 骨架与契约冻结（P4 加 Insight，P13 加 AgentStep）
# 依赖：pydantic
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """存活检查响应。"""

    status: str = "ok"


class CapabilitiesOut(BaseModel):
    """能力可用性响应。"""

    capabilities: dict[str, bool] = Field(default_factory=dict)
    descriptions: dict[str, str] = Field(default_factory=dict)
