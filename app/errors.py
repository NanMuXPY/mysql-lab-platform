"""errors.py — 统一异常体系。

所有业务异常继承 PlatformError，FastAPI 层（任务 3/4）可据此统一转
HTTP 状态码与错误响应，避免各模块异常类型零散、前端难处理。
"""

from __future__ import annotations


class PlatformError(Exception):
    """平台业务异常基类。message 面向日志，可直接展示给教师端。"""


class ProviderError(PlatformError):
    """Docker provider 操作失败（创建/销毁/查询容器）。

    调用方约定：捕获后必须写 instances.last_error 并归还端口租约。
    """


class PortPoolExhausted(PlatformError):
    """端口段全部租出。调用方可提示缩减班级规模或扩容端口段。"""


class NotFound(PlatformError):
    """资源不存在（实验单/实例/模板）。"""
