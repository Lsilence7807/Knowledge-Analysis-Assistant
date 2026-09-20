# 文件：app/stream.py
# 作用：SSE 帧编码与断连判定：/ask/stream 的帧序由 main.py 编排，这里只管拼帧与「客户端还在吗」
# 阶段：P17 流式输出
# 依赖：标准库 json、os
from __future__ import annotations

import json
import os

ENABLED = os.getenv("ENABLE_STREAM", "false").lower() == "true"  # §6：新增能力默认关
# 反代不许缓存、不许攒包：攒包会把首帧拖到整段结束，SSE 就退化成一次性返回
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(event: str, data: dict) -> str:
    """拼一帧 SSE：event + data + 空行收尾；中文不转义，浏览器直接读。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def canceled(request) -> bool:
    """客户端是否已断开。

    Starlette 只在 await request.is_disconnected() 时刷新这个标记，所以调用方每跨一段先 await 刷一次，
    这里只做同步读取——流水线在线程里、模型回调也是同步的，都没法 await。
    """
    return bool(getattr(request, "_is_disconnected", False))
