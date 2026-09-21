# 文件：backend/app/core/security.py
# 作用：单密码认证与限流：pbkdf2 校验口令、itsdangerous 签会话 cookie、slowapi 限流（401 与 429 口径）
# 阶段：F7 守卫与认证（兼 P11）
# 依赖：itsdangerous、slowapi、标准库 base64/hashlib/hmac/secrets、backend/app/core/config.py
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core import config

COOKIE_NAME = "kaa_session"
SESSION_MAX_AGE_S = 7 * 24 * 3600
PBKDF2_ROUNDS = 240_000
USER_ID = "owner"  # 单密码即单用户：cookie 里带的身份固定是这一个
LOGIN_LIMIT = "5/minute"  # §7 登录失败限流：定值，不吃 RATE_LIMIT_PER_MIN（那条给正常流量用）
# §4.3 里认证列「否」的四条路径：存活检查、能力清单、登录与登出
EXEMPT_PATHS = frozenset({"/health", "/capabilities", "/login", "/logout"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver", "testclient"})
# slowapi 没有「不限流」的写法：未开认证（本地自用）时用一个够大的数等价表达
LOCAL_UNLIMITED_PER_MIN = 1_000_000
# 没配 APP_PASSWORD_HASH 时的进程内密钥：cookie 只在本次进程里有效，正好当「配置缺失」的兜底
_FALLBACK_SECRET = secrets.token_hex(32)


def auth_enabled() -> bool:
    """认证是否生效：ENABLE_AUTH 打开且没被 AUTH_DISABLED 顶掉（本地默认关）。"""
    return bool(config.ENABLE_AUTH and not config.AUTH_DISABLED)


def password_configured() -> bool:
    """有没有配登录口令哈希：启动自检与登录端点都看它。"""
    return bool((config.APP_PASSWORD_HASH or "").strip())


def _secret() -> str:
    """cookie 签名密钥取口令哈希：改密码即让所有旧 cookie 一起失效。"""
    return (config.APP_PASSWORD_HASH or "").strip() or _FALLBACK_SECRET


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=_secret(), salt="kaa-session")


def global_limit() -> str:
    """每 IP 每分钟上限（§10 的 RATE_LIMIT_PER_MIN）：公开端按配置，本地自用不限流。

    做成可调用对象而不是常量：slowapi 每次请求现取，测试改 config 立刻生效。
    """
    return f"{config.RATE_LIMIT_PER_MIN}/minute" if auth_enabled() else f"{LOCAL_UNLIMITED_PER_MIN}/minute"


# 限流器是进程级单例：默认上限按请求时的配置算（见 global_limit），登录端点另挂 LOGIN_LIMIT
limiter = Limiter(key_func=get_remote_address, default_limits=[global_limit])


def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$轮数$盐$哈希` 形状的口令串：填进 APP_PASSWORD_HASH（环境变量或 config/local.json）。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str) -> bool:
    """核对口令：没配哈希、密码为空或口令串坏了都算不通过（不给空密码后门）。"""
    stored = (config.APP_PASSWORD_HASH or "").strip()
    if not stored or not password:
        return False
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac(algorithm.removeprefix("pbkdf2_"), password.encode(), salt.encode(), int(rounds))
    except ValueError:
        return False
    return hmac.compare_digest(base64.b64encode(digest).decode(), expected)


def secure_cookie(request: Request) -> bool:
    """公网强制 Secure（§7）：请求不是从回环地址来的就加 Secure，本地 http 调试不受影响。"""
    return (request.url.hostname or "") not in LOOPBACK_HOSTS


def login(request: Request, response: Response) -> str:
    """登录成功：签一张会话 cookie 写回响应（HttpOnly + SameSite=Lax），返回用户名。"""
    response.set_cookie(
        COOKIE_NAME,
        _serializer().dumps({"user": USER_ID}),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=secure_cookie(request),
        path="/",
    )
    return USER_ID


def logout(response: Response) -> None:
    """登出：清掉 cookie；签名是自包含的，服务端没有会话表要删。"""
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request) -> str | None:
    """取当前用户：未开认证给 None；开了认证但 cookie 缺失、过期或签名不对抛 401。"""
    if not auth_enabled():
        return None
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = _serializer().loads(token, max_age=SESSION_MAX_AGE_S)
            return str(payload.get("user") or USER_ID)
        except (BadSignature, SignatureExpired):
            pass  # 篡改与过期都按未登录处理，不细分原因（不给试探者反馈）
    raise HTTPException(status_code=401, detail="未登录或登录已过期，请先登录")


def require_auth(request: Request) -> None:
    """全局认证依赖（挂在 api_router 上）：免认证路径放行，其余走 current_user 的 401 口径。"""
    if request.url.path in EXEMPT_PATHS:
        return
    current_user(request)
