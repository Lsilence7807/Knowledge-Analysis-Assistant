# 文件：app/insight.py
# 作用：结果表 → 结构化结论（Insight）；数字要能追溯到结果表，否则写进 caveats
# 阶段：P4 洞察生成（K-009：findings 给行号即视为已追溯；K-018：标识符里的数字不算）
# 依赖：json、re、app/llm.py、app/schemas.py
from __future__ import annotations

import json
import re

from app import llm
from app.schemas import Insight

PROMPT_VERSION = "p4-2"  # 提示词改一次升一格；P10 做缓存时它要一起进缓存键
MAX_ROWS = 50  # 喂给模型的明细行数上限：够看形状，又不至于把上下文撑爆
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
IDENT = re.compile(r"[A-Za-z_]\w*")  # 标识符（表名、列名、数据集 id）：里面的数字不是结论里的数字


class InsightError(RuntimeError):
    """模型没给出可用的结论；code 是写进 degraded 的标签（insight:llm / insight:contract）。"""

    def __init__(self, message: str, code: str = "insight:contract"):
        super().__init__(message)
        self.code = code


SYSTEM = f"""你在解读一张数据分析的结果表（提示词版本 {PROMPT_VERSION}）。
规则：
1. 只依据给出的结果表说话；findings 里要用 "row" 指出这句话读的是结果表第几行（0 基，行号从上面的明细行数起），可以再给 "column" 写列名。
2. 数字要么来自表里，要么是从指认的那些行算出来的（合计、比率、环比都算）；既不在表里、也指不出行的数字不要写。
3. 表里支持不了的判断不要写；证据不足就写进 caveats，并把 confidence 压低。
4. findings 是表里读得出的事实，suggestions 是下一步该做什么，两者都要给依据。
5. 表与问题无关或看不出结论时，summary 直说，confidence 用 low。
输出 JSON：{{"summary": "一句话结论", "findings": [{{"title": "...", "detail": "...", "metric": "...", "direction": "up|down|flat", "row": 0, "column": "..."}}],
 "anomalies": [{{"column": "...", "row_hint": "...", "value": 0, "reason": "..."}}],
 "suggestions": [{{"action": "...", "rationale": "..."}}], "confidence": "low|medium|high", "caveats": ["..."]}}"""


def summarize(question: str, result: dict, profile: dict, prior: list[str]) -> Insight:
    """结果表 → Insight；模型不可用或输出不合契约时抛 InsightError（带 degraded 标签）。"""
    user = _user_prompt(question, result, profile, prior)
    try:
        insight = llm.chat_json(SYSTEM, user, Insight)
    except llm.LLMUnavailable as exc:
        raise InsightError(str(exc), "insight:llm") from exc
    except llm.LLMError as exc:  # chat_json 内部已按契约重试 1 次
        raise InsightError(str(exc), "insight:contract") from exc
    invented = _unsupported_numbers(insight, result)
    if invented:
        insight.caveats = [
            *insight.caveats,
            f"以下数字在结果表里找不到，可能是模型编的：{'、'.join(invented)}",
        ]
    return insight


def _user_prompt(question: str, result: dict, profile: dict, prior: list[str]) -> str:
    """问题 + 结果表（限 MAX_ROWS 行）+ 数据画像，拼成给模型的一段说明。"""
    rows = result.get("rows") or []
    columns = [str(name) for name in (result.get("columns") or [])]
    lines = [
        f"数据集：{profile.get('table', '')}（{profile.get('rows', '?')} 行，{profile.get('cols', '?')} 列）",
        f"问题：{question}",
        f"SQL：{result.get('sql', '')}",
        f"结果表：{len(rows)} 行（已截断：{bool(result.get('truncated'))}），列：{'、'.join(columns)}",
    ]
    lines += [json.dumps(row, ensure_ascii=False) for row in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        lines.append(f"（只给了前 {MAX_ROWS} 行）")
    if prior:
        lines.append("补充上下文：" + "；".join(prior))
    return "\n".join(lines)


def _unsupported_numbers(insight: Insight, result: dict) -> list[str]:
    """结论里出现、又追溯不到结果表的数字；设计硬规则要求这类数字必须写进 caveats。

    K-009：findings 逐条都给出行号时，整段结论视为已追溯（合计、环比这类算式结果表里本来就没有原文）。
    K-018：兜底的字面比对先把标识符剔掉，表名 `ds_<id>`、列名里的数字不算可疑数字。
    """
    rows = result.get("rows") or []
    if insight.findings and all(_cited(item, rows) for item in insight.findings):
        return []
    known = {_as_number(value) for row in rows for value in row}
    known.discard(None)
    text = " ".join(
        [insight.summary]
        + [f"{item.title} {item.detail}" for item in insight.findings]
        + [item.action for item in insight.suggestions]
    )
    return sorted({token for token in NUMBER.findall(IDENT.sub(" ", text)) if _as_number(token) not in known})


def _cited(finding, rows: list) -> bool:
    """这条 finding 是否指认了结果表里真实存在的行号。"""
    return isinstance(finding.row, int) and 0 <= finding.row < len(rows)


def _as_number(value):
    """把单元格或文本片段转成可比较的数字（两位小数）；不是数字就返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
