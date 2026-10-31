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
    # 用 .get 而不是 []：学生 token 里没有 uid（学生不在 users 表），
    # 直接取键会 KeyError → 500，而正确语义是"角色不符 → 403"
    return {"id": payload.get("uid") or 0, "login": payload.get("sub", ""),
            "role": payload.get("role", ""), "name": payload.get("name", "")}


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


# ---------------------------------------------------------------------------
# 学生端凭证（「我的错题」页）
# ---------------------------------------------------------------------------
# 为什么不为学生建账号：一学期几十个教学班、上千学号，逐个建号发密码是额外运维；
# 而学生手里本来就有平台发的数据库密码（随机 12 位，CSV 分发）。
# 用「学号 + 数据库密码」登录，密码即凭证，零额外账号体系。
def create_student_token(student_id: str, lab_id: int, instance_id: int) -> str:
    """学生 token 把 sid/lid/iid 写进 payload，服务端据此只返回该学生自己的数据。"""
    from . import config
    now = datetime.now(timezone.utc)
    payload = {
        "sub": student_id, "sid": student_id, "lid": lab_id, "iid": instance_id,
        "role": "student", "name": student_id,
        "iat": now, "exp": now + timedelta(hours=6),
    }
    return jwt.encode(payload, config.APP_SECRET, algorithm=_JWT_ALGO)


def get_student_ctx(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """学生上下文依赖：非学生 token 一律 403（教师 token 不能当学生用）。

    越权防护的关键：所有学生接口只认这里返回的 student_id / lab_id，
    绝不接受请求参数里的学号——否则改个 URL 就能看别人的错题。
    """
    if creds is None:
        raise HTTPException(status_code=401, detail="缺少认证凭证")
    payload = decode_token(creds.credentials)
    if payload.get("role") != "student" or not payload.get("sid"):
        raise HTTPException(status_code=403, detail="需要学生凭证")
    return {"student_id": payload["sid"], "lab_id": payload["lid"],
            "instance_id": payload["iid"]}
