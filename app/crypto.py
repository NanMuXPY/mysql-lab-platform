"""crypto.py — 学生密码对称加密（交接文档第四节：不存明文）。

用 cryptography 的 Fernet（AES-128-CBC + HMAC-SHA256 认证加密）：
- 密钥由环境变量 APP_SECRET 派生（sha256），不落盘
- encrypt 输出带时间戳与认证标签，防篡改
- 教师端只在创建时展示一次明文，之后只能重置
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .errors import PlatformError


class SecretNotConfigured(PlatformError):
    """APP_SECRET 未配置，拒绝加解密（宁可失败也不落明文）。"""


def _fernet() -> Fernet:
    if not config.APP_SECRET:
        raise SecretNotConfigured(
            "APP_SECRET 未设置，拒绝加密存储。请在服务器 .env 配置 32+ 字节随机值"
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(config.APP_SECRET.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise PlatformError("解密失败：APP_SECRET 与加密时不一致，或密文被篡改") from e


def constant_time_eq(a: str, b: str) -> bool:
    """常量时间比较，防时序侧信道（登录路径用）。"""
    return hmac.compare_digest(a.encode(), b.encode())
