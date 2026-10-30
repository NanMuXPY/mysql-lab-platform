"""auth.py — 登录、JWT 签发与校验、角色判断（交接文档第三节任务 3）。

- 密码校验：PBKDF2（models.hash_password / verify_password）
- JWT：HS256，payload 含 sub/uid/role/name/exp，默认 12 小时
- FastAPI 依赖注入：get_current_user → require_role("teacher") 形式组合
- 错误语义：无/坏 token → 401；token 有效但角色不符 → 403（验收标准）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import models
from .errors import PlatformError

_JWT_ALGO = "HS256"
_TOKEN_HOURS = 12
bearer_scheme = HTTPBearer(auto_error=False)


def create_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["login"],
        "uid": user["id"],
        "role": user["role"],
        "name": user["name"],
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_HOURS),
    }
    from . import config
    return jwt.encode(payload, config.APP_SECRET, algorithm=_JWT_ALGO)


def decode_token(token: str) -> dict:
    from . import config
    try:
        return jwt.decode(token, config.APP_SECRET, algorithms=[_JWT_ALGO])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="无效凭证") from e


def login(login_name: str, password: str) -> dict:
    """校验登录，成功返回 {token, role, name}，失败抛 401。"""
    user = models.get_user_by_login(login_name)
    if not user or not models.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {"token": create_token(user), "role": user["role"], "name": user["name"]}


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """从 Authorization: Bearer 解析当前用户，失败 401。"""
    if creds is None:
        raise HTTPException(status_code=401, detail="缺少认证凭证")
    payload = decode_token(creds.credentials)
    return {"id": payload["uid"], "login": payload["sub"],
            "role": payload["role"], "name": payload["name"]}


def require_role(*roles: str):
    """角色门禁依赖工厂：require_role('teacher') 挂在路由上即完成权限收敛。"""
    def dep(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"需要 {'/'.join(roles)} 角色，当前为 {user['role']}",
            )
        return user
    return dep
