# 文件：backend/app/api/routes/auth.py
# 作用：登录与登出端点（§4.3 的 P11 行）：口令校验 + 签名 cookie；登录失败限流记在 slowapi 里
# 阶段：F7 守卫与认证（兼 P11）
# 依赖：fastapi、app/core/{config,security}.py、app/schemas/__init__.py
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.core import security
from app.schemas import LoginIn

router = APIRouter()


@router.post("/login")
@security.limiter.limit(security.LOGIN_LIMIT)
def login(request: Request, response: Response, payload: LoginIn) -> dict:
    """单密码登录：口令对了签发 cookie，错了 401；同一 IP 每分钟只放 LOGIN_LIMIT 次，超出 429。"""
    if not security.password_configured():
        # 没配口令就没有可登录的账号：给 503 而不是 401，免得像是「密码错了」
        raise HTTPException(status_code=503, detail="服务端还没设登录口令（APP_PASSWORD_HASH）")
    if not security.verify_password(payload.password):
        raise HTTPException(status_code=401, detail="密码不正确")
    return {"user": security.login(request, response)}


@router.post("/logout")
def logout(response: Response) -> dict:
    """登出：清掉 cookie；本来就没登录也返回 200（幂等）。"""
    security.logout(response)
    return {"ok": True}
