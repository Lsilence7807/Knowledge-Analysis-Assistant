# 文件：backend/app/services/llm.py
# 作用：唯一模型出入口——LiteLLM 管厂商/模型组/回退/重试/结构化输出；`LLM_BACKEND=direct` 回落原 OpenAI 单厂商路径
# 阶段：F3 模型层换 LiteLLM（chat_json / chat_tools / stream_text 的契约与签名不变）
# 依赖：json、litellm、openai、backend/app/core/config.py、backend/app/services/llm_models.py
from __future__ import annotations

import json
import time

from app.core import config, guardrail
from app.services import llm_models, trace

RETRY = 1  # 输出不合契约时的重试次数，第二次仍不合契约就降级
MAX_TEXT = 500  # 模型原文进报错与日志前的截断长度

_router = None  # LiteLLM Router 缓存：配置变了按指纹重建（见 _get_router）
_router_key = ""

guardrail.install()  # §7：请求进模型前统一过一遍注入防护（本地扫描 + LiteLLM guardrail hook）


class LLMUnavailable(RuntimeError):
    """模型这条路不可用（缺密钥、缺 profile、direct 后端不支持的厂商）。"""


class LLMError(RuntimeError):
    """模型调用失败或输出不符合契约。"""


def ensure_ready(model_id: str | None = None) -> dict:
    """取模型 profile 并确认密钥在；缺任一条件抛 LLMUnavailable。"""
    model = llm_models.resolve(model_id)
    if not config.api_key(model):
        raise LLMUnavailable(
            f"未配置模型密钥（profile {model.get('id')}）：在页面配一次，"
            "或写 config/local.json 的 api_keys，也可设该 profile 声明的环境变量"
        )
    if config.LLM_BACKEND == "direct" and model.get("provider") != "openai_compatible":
        raise LLMUnavailable(f"direct 后端只支持 openai_compatible：{model.get('provider')}")
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


def embed(texts: list[str]) -> list[list[float]]:
    """文本向量（§4.2）：厂商调用交 LiteLLM 的 embedding；缺 profile 或缺密钥抛 LLMUnavailable 让调用方回退。"""
    payload = [str(text) for text in texts]
    if not payload:
        return []
    profile = llm_models.embedding_profile()
    key = config.api_key(profile or {})
    if not profile or not key:
        raise LLMUnavailable(
            "没有可用的 embedding profile：在 config/models.json 的 embedding 块里配一个，"
            "或给某个 profile 的 purpose 加 embedding"
        )
    import litellm

    params = {
        "model": llm_models.litellm_model(profile),
        "api_key": key,
        "input": payload,
        "timeout": profile.get("timeout_s", 30),
    }
    if profile.get("base_url"):
        params["api_base"] = profile["base_url"]
    started = time.perf_counter()
    try:
        response = litellm.embedding(**params)
    except Exception as exc:  # 网络、鉴权、模型名不对一律当这一层不可用
        trace.record("", "llm.embed", model_id=profile.get("id"), ms=trace.elapsed_ms(started), ok=False)
        raise LLMError(f"embedding 调用失败：{_clip(exc)}") from exc
    tokens_in, _, cost = trace.usage_of(response)
    trace.record(
        "", "llm.embed", model_id=profile.get("id"), tokens_in=tokens_in, cost=cost, ms=trace.elapsed_ms(started)
    )
    return [list(item["embedding"]) for item in response.data]


def _complete(messages: list[dict], model: dict) -> str:
    """取原始文本的唯一网络出口；测试直接替换它（保持两参签名）。"""
    started = time.perf_counter()
    if config.LLM_BACKEND == "direct":
        text = _direct_complete(messages, model)
        trace.record("", "llm.complete", model_id=model.get("id"), ms=trace.elapsed_ms(started))
        return text
    try:
        response = _get_router().completion(
            model=_group_name(model),
            messages=messages,
            response_format={"type": "json_object"},
            num_retries=0,  # 重试策略由 chat_json 统一管，这里不叠一层
        )
    except Exception:
        trace.record("", "llm.complete", model_id=model.get("id"), ms=trace.elapsed_ms(started), ok=False)
        raise
    tokens_in, tokens_out, cost = trace.usage_of(response)
    trace.record(
        "",
        "llm.complete",
        model_id=model.get("id"),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        ms=trace.elapsed_ms(started),
    )
    return response.choices[0].message.content or ""


def chat_tools(messages: list[dict], tools: list[dict], model_id: str | None = None) -> dict:
    """让模型决定下一步：返回 {"content": str, "tool_calls": [{"id","name","arguments"}]}。

    arguments 已解析成 dict；模型给的不是合法 JSON 时当空参处理，由工具层报「参数不匹配」让它改。
    """
    model = ensure_ready(model_id)
    started = time.perf_counter()
    if config.LLM_BACKEND == "direct":
        message = (
            _client(model)
            .chat.completions.create(model=model["model"], messages=messages, tools=tools, tool_choice="auto")
            .choices[0]
            .message
        )
        trace.record("", "llm.tools", model_id=model.get("id"), ms=trace.elapsed_ms(started))
    else:
        try:
            response = _get_router().completion(
                model=_group_name(model), messages=messages, tools=tools, tool_choice="auto", num_retries=0
            )
        except Exception:
            trace.record("", "llm.tools", model_id=model.get("id"), ms=trace.elapsed_ms(started), ok=False)
            raise
        tokens_in, tokens_out, cost = trace.usage_of(response)
        trace.record(
            "",
            "llm.tools",
            model_id=model.get("id"),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            ms=trace.elapsed_ms(started),
        )
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


async def stream_text(system: str, user: str, model_id: str | None = None):
    """流式取模型文本：逐段 yield 增量；调用失败抛 LLMError（不重试——吐出去的片段收不回来）。

    流式必须用异步客户端：断连时 Starlette 取消协程能真把上游请求掐掉。
    """
    model = ensure_ready(model_id)
    started = time.perf_counter()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        if config.LLM_BACKEND == "direct":
            stream = await _client(model, async_=True).chat.completions.create(
                model=model["model"], messages=messages, stream=True
            )
        else:
            stream = await _get_router().acompletion(
                model=_group_name(model), messages=messages, stream=True, num_retries=0
            )
        async for chunk in stream:
            piece = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if piece:
                yield piece
    except Exception as exc:  # 网络、鉴权、限流一律降级，与 chat_json 同口径
        trace.record("", "llm.stream", model_id=model.get("id"), ms=trace.elapsed_ms(started), ok=False)
        raise LLMError(f"模型调用失败：{_clip(exc)}") from exc
    # 流式片段里拿不到完整用量，这里只记耗时；成本由非流式的 llm.complete / llm.tools 记账
    trace.record("", "llm.stream", model_id=model.get("id"), ms=trace.elapsed_ms(started))


def _get_router():
    """按当前 profile 与密钥建 LiteLLM Router（组内挑、失败按回退链走），配置变了自动重建。"""
    import hashlib
    import json as _json

    import litellm

    global _router, _router_key
    deployments = llm_models.deployments()
    key = hashlib.sha256(_json.dumps(deployments, sort_keys=True, default=str).encode()).hexdigest()
    if _router is None or key != _router_key:
        _router = litellm.Router(
            model_list=deployments,
            fallbacks=llm_models.fallbacks() or None,
            num_retries=1,  # 同组内换一个 deployment 再试一次
            set_verbose=False,
        )
        _router_key = key
    return _router


def _group_name(model: dict) -> str:
    """挑 LiteLLM 的模型组：purpose 组里还有别的 profile 就用组名（组内挑 + 回退），否则用 profile id。"""
    groups = llm_models.purpose_groups()
    for purpose in model.get("purpose") or []:
        if len(groups.get(str(purpose), [])) > 1:
            return str(purpose)
    return str(model.get("id"))


# ---- direct 回落路径：原 OpenAI 单厂商客户端（LiteLLM 出问题时的退路）----
def _direct_complete(messages: list[dict], model: dict) -> str:
    response = _client(model).chat.completions.create(
        model=model["model"], messages=messages, response_format={"type": "json_object"}
    )
    return response.choices[0].message.content or ""


def _client(model: dict, async_: bool = False):
    """建 OpenAI 兼容客户端（direct 路径用）。"""
    from openai import AsyncOpenAI, OpenAI

    factory = AsyncOpenAI if async_ else OpenAI
    return factory(
        api_key=config.api_key(model),
        base_url=model.get("base_url"),
        timeout=model.get("timeout_s", 30),
        max_retries=0,
    )


def _clip(text) -> str:
    """截断模型原文，避免超长文本撑爆报错信息与日志。"""
    flat = " ".join(str(text).split())
    return f"{flat[:MAX_TEXT]}…" if len(flat) > MAX_TEXT else flat
