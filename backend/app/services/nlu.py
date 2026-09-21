# 文件：backend/app/services/nlu.py
# 作用：单跳 自然语言 → SQL；P13 之后不删，作为 ENABLE_AGENT=false 时的降级通道
# 阶段：P3 自然语言转 SQL
# 依赖：pydantic、langchain-core、backend/app/services/llm.py
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from app.services import llm

PROMPT_VERSION = "p3-1"  # 提示词改一次升一格；P10 做缓存时它要一起进缓存键


class NLUError(RuntimeError):
    """模型没给出可用的 SQL（指代不明、缺列等），信息可直接回给用户。"""


class SqlDraft(BaseModel):
    """模型必须返回的形状。"""

    sql: str = ""
    need_clarify: bool = False
    clarify: str = ""
    reason: str = ""


# 提示词走 ChatPromptTemplate（§11 的 LangChain 半截在 F9 补齐：F3 当时只落了 LiteLLM）
_SYSTEM_PROMPT = ChatPromptTemplate.from_template(
    """你在为一个只读的 DuckDB 数据分析助手写 SQL（提示词版本 {version}）。
规则：
1. 只输出一条 SELECT（可以带 WITH 子查询），不要分号、不要注释、不要任何写操作。
2. 只用下面给出的表和列，列名原样照抄，需要时用双引号包起来。
3. 能聚合就聚合，明细查询要限制行数（最多 5000 行），必要时加 LIMIT。
4. 问题指代不明，或数据里缺少所需列时不要猜：need_clarify 设为 true，并在 clarify 里说清缺什么。
输出 JSON：{{"sql": "...", "need_clarify": false, "clarify": "", "reason": "一句话思路"}}"""
)

_USER_PROMPT = ChatPromptTemplate.from_template(
    """表名：{{ table }}
规模：{{ rows }} 行
列：{{ columns }}
问题：{{ question }}{{ context_line }}""",
    template_format="jinja2",
)


def _system() -> str:
    """渲染系统提示词：版本号进正文，与缓存的 PROMPT_VERSION 一起走。"""
    return _SYSTEM_PROMPT.format_messages(version=PROMPT_VERSION)[0].content


def to_sql(question: str, profile: dict, context: list[str]) -> str:
    """问题 + 数据画像 → 一条只读 SELECT；给不出可执行 SQL 时抛 NLUError。"""
    draft = llm.chat_json(_system(), _user_prompt(question, profile, context), SqlDraft)
    sql = draft.sql.strip()
    # 指代不明宁可让用户补一句，也不要猜着出一张错表
    if draft.need_clarify or not sql:
        raise NLUError(draft.clarify or "问题指代不明确，请补充口径后重问")
    return sql


def _user_prompt(question: str, profile: dict, context: list[str]) -> str:
    """用户侧提示词：模板渲染（渲染结果与改造前逐字一致）。"""
    columns = profile.get("columns") or []
    return _USER_PROMPT.format_messages(
        table=profile.get("table", ""),
        rows=profile.get("rows", "?"),
        columns="、".join(
            f"{column.get('name')}({column.get('dtype')}，空值 {column.get('null_count', 0)})" for column in columns
        ),
        question=question,
        context_line=("\n补充上下文：" + "；".join(context)) if context else "",
    )[0].content
