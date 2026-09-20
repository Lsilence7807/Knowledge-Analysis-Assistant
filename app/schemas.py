# 文件：app/schemas.py
# 作用：HTTP 边界的 pydantic 模型，接口契约以本文件为准
# 阶段：P0 骨架与契约冻结（P4 加 Insight，K-013 加请求体模型，K-009 给 Finding 加引用字段，P15 加 SandboxIn）
# 依赖：pydantic
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """存活检查响应。"""

    status: str = "ok"


class CapabilitiesOut(BaseModel):
    """能力可用性响应。"""

    capabilities: dict[str, bool] = Field(default_factory=dict)
    descriptions: dict[str, str] = Field(default_factory=dict)


class Finding(BaseModel):
    """结果表里读得出的一条事实；row/column 指认它读的是结果表的哪一行哪一列（K-009）。"""

    title: str
    detail: str = ""
    metric: str = ""
    direction: str = ""
    row: int | None = None  # 结果表里的行号（0 基）；填了就算这条已追溯到表
    column: str = ""  # 取数用的列名，仅供参考，不做校验


class Suggestion(BaseModel):
    """下一步该做什么。"""

    action: str
    rationale: str = ""


class Insight(BaseModel):
    """结果表的解读；字段与设计 §3.4 一致，缺字段即视为不合契约（由调用方降级）。"""

    summary: str
    findings: list[Finding]
    anomalies: list[dict]
    suggestions: list[Suggestion]
    confidence: Literal["low", "medium", "high"]
    caveats: list[str]


class QueryIn(BaseModel):
    """POST /query 请求体：一条只读 SQL。"""

    sql: str


class StatsIn(BaseModel):
    """POST /stats 请求体：数据集与要统计的列（缺省即全部列）。"""

    dataset_id: str
    columns: list[str] | None = None


class AskIn(BaseModel):
    """POST /ask 请求体：提问；session_id 可空。"""

    dataset_id: str
    question: str
    session_id: str = ""


class InsightIn(BaseModel):
    """POST /insight 请求体：数据集与要解读的 SQL；question 只用于措辞。"""

    dataset_id: str
    sql: str
    question: str = ""


class SandboxIn(BaseModel):
    """POST /sandbox/run 请求体：要执行的 pandas 代码；dataset_id 非空时把该数据集只读副本注入为 df。"""

    code: str
    dataset_id: str = ""
