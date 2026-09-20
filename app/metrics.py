# 文件：app/metrics.py
# 作用：指标语义层：把提问里的指标名与别名解析成指标 id（口径的唯一来源是 config/metrics.json），
#       并把「像指标但没定义过」的说法标出来——只标注，绝不臆造公式
# 阶段：P15 沙箱代码执行与指标语义层
# 依赖：标准库 json、re、pathlib、app/config.py
from __future__ import annotations

import json
import re
from pathlib import Path

from app import config

METRICS_CONFIG = Path(config.BASE_DIR) / "config" / "metrics.json"

# 未定义指标的识别是启发式的（ADR-20：口径只认定义文件）：认「中文词 + 口径后缀」，不猜公式
_SUFFIXES = ("占比", "客单价", "成本", "利润", "收入", "增速", "毛利", "净利", "额", "率", "量", "数", "比", "价")
_CANDIDATE = re.compile("[一-龥A-Za-z]{1,8}?(?:" + "|".join(_SUFFIXES) + ")")


def definitions() -> list[dict]:
    """读 config/metrics.json 的指标口径列表；文件缺失或坏掉回空列表，不抛错。"""
    try:
        loaded = json.loads(METRICS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = loaded.get("definitions") if isinstance(loaded, dict) else loaded
    return [item for item in items or [] if isinstance(item, dict) and item.get("id")]


def _surfaces(item: dict) -> list[str]:
    """一个指标对外出现过的全部写法：id、名称与别名。"""
    aliases = [str(alias) for alias in item.get("aliases") or []]
    return [str(item.get("id") or ""), str(item.get("name") or ""), *aliases]


def _public(item: dict) -> dict:
    """口径的对外字段：只给 id、名称、别名、公式、单位与口径说明，够模型引用即可。"""
    return {key: item.get(key) for key in ("id", "name", "aliases", "formula_sql", "unit", "owner", "note")}


def resolve_metrics(text: str) -> list[dict]:
    """把文本里出现的指标名解析成指标 id（别名同样算命中），按出现位置排序；没命中回空列表。"""
    question = text or ""
    hits: list[tuple[int, dict]] = []
    for item in definitions():
        found = sorted(
            (question.find(surface), surface) for surface in _surfaces(item) if surface and surface in question
        )
        if found:
            # 同一指标在文里出现多种写法时，取最先出现的那种当 matched
            hits.append((found[0][0], {**_public(item), "matched": found[0][1]}))
    return [hit for _, hit in sorted(hits, key=lambda pair: pair[0])]


def undefined_terms(text: str) -> list[str]:
    """挑出文本里像指标却没在 config/metrics.json 定义过的词；只做标注，不编公式。"""
    question = text or ""
    known = [surface for item in definitions() for surface in _surfaces(item) if surface]
    found: list[str] = []
    for match in _CANDIDATE.finditer(question):
        term = match.group(0)
        # 与定义里的任一写法有包含关系就算定义过（「销售额」里切出的「售额」不算新指标）
        if any(surface in term or term in surface for surface in known):
            continue
        if term not in found:
            found.append(term)
    return found
