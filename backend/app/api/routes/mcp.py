# 文件：backend/app/api/routes/mcp.py
# 作用：MCP 端点：列工具与调用白名单内工具（server 起不来时 503）
# 阶段：F1 后端骨架（原 app/main.py 的 /mcp/* 端点原样搬来）
# 依赖：fastapi、app/api/deps.py
from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_capability

router = APIRouter()


@router.get("/mcp/tools")
def mcp_tools(module=Depends(require_capability("mcp"))) -> dict:
    """列配置里那台 MCP server 的工具（含是否在白名单内）；开关关闭或 server 起不来时 503。"""
    return module.list_tools()


@router.post("/mcp/call")
def mcp_call(payload: dict = Body(default={}), module=Depends(require_capability("mcp"))) -> dict:
    """调一个白名单内的 MCP 工具，返回按不可信数据包裹过的结果；工具被拒或 server 不可用不抛 500。"""
    try:
        return module.call_tool(str(payload.get("tool") or ""), payload.get("arguments") or {})
    except module.McpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
