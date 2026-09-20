# 文件：app/llm.py
# 作用：唯一模型出入口：结构化 JSON 输出（内部按 provider 分支，P9 加厂商时只加分支）
# 阶段：P3 自然语言转 SQL（chat_tools 留到 P13）
# 依赖：json、openai、app/config.py、app/models.py
from __future__ import annotations

import json

from app import config, models

RETRY = 1  # 输出不合契约时的重试次数，第二次仍不合契约就降级
MAX_TEXT = 500  # 模型原文进报错与日志前的截断长度


class LLMUnavailable(RuntimeError):
    """模型这条路不可用（缺密钥、缺 profile、provider 不支持）。"""


class LLMError(RuntimeError):
    """模型调用失败或输出不符合契约。"""


def ensure_ready(model_id: str | None = None) -> dict:
    """取模型 profile 并确认密钥在；缺任一条件抛 LLMUnavailable。"""
    model = models.resolve(model_id)
    if not config.api_key(model):
        raise LLMUnavailable(
            f"未配置模型密钥（profile {model.get('id')}）：在页面配一次，"
            "或写 config/local.json 的 api_keys，也可设该 profile 声明的环境变量"
        )
    if model.get("provider") != "openai_compatible":
        raise LLMUnavailable(f"暂不支持的 provider：{model.get('provider')}")
    return model


def chat_json(system: str, user: str, schema: type, model_id: str | None = None):
    """让模型返回符合 schema 的 JSON 对象；非法输出重试 1 次后抛 LLMError。"""
    model = ensure_ready(model_id)
    messages = [
        {
            "role": "system",
            "content": f"{system}\n\n必须只输出 JSON，字段定义如下："
            f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}",
        },
        {"role": "user", "content": user},
    ]
    last = ""
    for _ in range(RETRY + 1):
        try:
            text = _complete(messages, model)
        except Exception as exc:  # 网络、鉴权、限流一律降级，不重试
            raise LLMError(f"模型调用失败：{_clip(exc)}") from exc
        try:
            return schema.model_validate_json(text)
        except ValueError as exc:  # JSON 不合法与字段不符都走这里
            last = f"{exc}；原文：{_clip(text)}"
    raise LLMError(f"模型输出不符合契约：{last}")


def _complete(messages: list[dict], model: dict) -> str:
    """调用 OpenAI 兼容端点拿原始文本；唯一网络出口，测试直接替换它。"""
    from openai import OpenAI

    client = OpenAI(
        api_key=config.api_key(model),
        base_url=model.get("base_url"),
        timeout=model.get("timeout_s", 30),
        max_retries=0,  # 重试策略由 chat_json 统一管，这里不叠一层
    )
    response = client.chat.completions.create(
        model=model["model"], messages=messages, response_format={"type": "json_object"}
    )
    return response.choices[0].message.content or ""


def chat_tools(messages: list[dict], tools: list[dict], model_id: str | None = None) -> dict:
    """让模型决定下一步：返回 {"content": str, "tool_calls": [{"id","name","arguments"}]}。

    arguments 已解析成 dict；模型给的不是合法 JSON 时当空参处理，由工具层报「参数不匹配」让它改。
    """
    model = ensure_ready(model_id)
    client = _client(model)
    response = client.chat.completions.create(model=model["model"], messages=messages, tools=tools, tool_choice="auto")
    message = response.choices[0].message
    calls: list[dict] = []
    for call in message.tool_calls or []:
        try:
            parsed = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            parsed = {}
        calls.append(
            {
                "id": call.id,
                "name": call.function.name,
                "arguments": parsed if isinstance(parsed, dict) else {},
            }
        )
    return {"content": message.content or "", "tool_calls": calls}


def _client(model: dict):
    """建 OpenAI 兼容客户端；_complete 里的旧副本按阶段隔离规则不动，新代码走这里。"""
    from openai import OpenAI

    return OpenAI(
        api_key=config.api_key(model),
        base_url=model.get("base_url"),
        timeout=model.get("timeout_s", 30),
        max_retries=0,
    )


def _clip(text) -> str:
    """截断模型原文，避免超长文本撑爆报错信息与日志。"""
    flat = " ".join(str(text).split())
    return f"{flat[:MAX_TEXT]}…" if len(flat) > MAX_TEXT else flat
