# 文件：backend/app/core/guardrail.py
# 作用：外部正文的注入防护统一入口（§7）：行级指令剥离、命中留痕，并挂 LiteLLM guardrail hook
# 阶段：F8（兼 P16，收敛 K-031/K-033）
# 依赖：标准库 logging、re；backend/app/services/store.py；litellm（可选，缺了只做本地扫描）
from __future__ import annotations

import logging
import re

from app.services import store

logger = logging.getLogger(__name__)

# §7 口径：知识库片段、上传文档、MCP 返回、技能正文都算不可信。命中只剥掉那一行并留痕，
# 不整条丢弃（正文本身是用户要看的资料）；语义级判定挂在 LiteLLM 的 guardrail hook 上。
_PATTERNS = (
    r"忽略(以上|之前|上述|前面)",
    r"无视(以上|之前|上述|前面)",
    r"(ignore|disregard)\s+(all\s+)?(previous|above|prior)",
    r"(从现在起|接下来|现在)\s*(你|请)?\s*(是|扮演|充当|作为)\s*(一个)?\s*(系统|管理员|开发者模式)",
    r"(输出|告诉我|发给我|打印)[^。\n]{0,12}(你的)?(系统提示词|系统提示|system\s*prompt|密钥|口令|密码|api[_ ]?key)",
    r"(system\s*prompt|开发者模式|developer mode)\s*[:：]",
)
_INSTRUCTION_LIKE = re.compile("|".join(_PATTERNS), re.IGNORECASE)

_GUARD = None


def finds_injection(text: str) -> str | None:
    """扫描外部正文，返回命中的那一行（没命中给 None）；判定口径只有这一处，别再各写一套正则。"""
    for line in (text or "").splitlines():
        if _INSTRUCTION_LIKE.search(line):
            return line.strip()[:160]
    return None


def sanitize(text: str) -> str:
    """丢掉「指挥模型」的行，其余正文照留：kb 片段、MCP 返回、技能正文进上下文前都走它。"""
    kept = [line for line in (text or "").splitlines() if not _INSTRUCTION_LIKE.search(line)]
    return "\n".join(kept).strip()


def check(text: str, source: str) -> str | None:
    """统一闸门：命中就记 capability_log 并回原因（K-031/K-033 要的「命中留痕」在这一处收口）。"""
    hit = finds_injection(text)
    if hit is None:
        return None
    detail = f"{source} 命中注入特征：{hit}"
    store.log_capability("guardrail", "error", detail)
    return detail


def guard(text: str, source: str) -> str:
    """sanitize + 留痕一次做完：调用方拿到的正文已经剥过指令行，命中也已经进了 capability_log。"""
    check(text, source)
    return sanitize(text)


def _messages_text(messages: list) -> list[str]:
    """把 messages 里的正文抽出来（content 可能是字符串或分段列表），只扫正文，不看角色与工具名。"""
    texts: list[str] = []
    for message in messages or []:
        content = (message or {}).get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts


def hook():
    """惰性建一个 LiteLLM guardrail hook（进程内单例）：请求进模型前再扫一遍 messages。"""
    global _GUARD
    if _GUARD is None:
        from litellm.integrations.custom_logger import CustomLogger

        class InjectionGuardrail(CustomLogger):
            """LiteLLM 的输入侧守卫：命中即抛，谁调用谁负责把它当失败处理（§7 的「拒绝」）。"""

            async def async_moderation_hook(self, data, user_api_key_dict=None, call_type=None):  # noqa: ANN001, ARG002
                for text in _messages_text((data or {}).get("messages")):
                    reason = check(text, "模型请求")
                    if reason:
                        raise ValueError(reason)

        _GUARD = InjectionGuardrail()
    return _GUARD


def install() -> None:
    """把 hook 挂上 LiteLLM（幂等）；litellm 不在就静默跳过——退化形态是「只做本地扫描」。"""
    try:
        import litellm
    except ImportError:  # pragma: no cover - 依赖缺失时功能整体降级
        return
    guard_hook = hook()
    callbacks = getattr(litellm, "callbacks", None)
    if isinstance(callbacks, list) and guard_hook not in callbacks:
        callbacks.append(guard_hook)
