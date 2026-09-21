# 文件：backend/app/services/stream.py
# 作用：SSE 帧与断连判定：帧编码交 sse-starlette（ServerSentEvent），这里只管造帧与「客户端还在吗」
# 阶段：P17 流式输出；F7 手写分帧换 sse-starlette（帧契约 §4.11 逐字不变）
# 依赖：sse_starlette、标准库 json、os
from __future__ import annotations

import json
import os

from sse_starlette import ServerSentEvent

ENABLED = os.getenv("ENABLE_STREAM", "false").lower() == "true"  # §6：新增能力默认关
# sse-starlette 默认用 \r\n 分行，而帧契约与现有前端按 \n 分帧，这里钉死分隔符
SEP = "\n"


def sse(event: str, data: dict) -> ServerSentEvent:
    """造一帧 SSE：事件名 + JSON data；中文不转义，编码与分帧交给 sse-starlette。"""
    return ServerSentEvent(event=event, data=json.dumps(data, ensure_ascii=False, default=str), sep=SEP)


def canceled(request) -> bool:
    """客户端是否已断开。

    Starlette 只在 await request.is_disconnected() 时刷新这个标记，所以调用方每跨一段先 await 刷一次，
    这里只做同步读取——流水线在线程里、模型回调也是同步的，都没法 await。
    """
    return bool(getattr(request, "_is_disconnected", False))
