"""security.py — 安全基线：登录失败限流 + 安全响应头 + 请求耗时日志（v1.0.2）。

这三件事都是"内网测试完全看不出问题、一挂公网就要命"的缺口：

1. **登录失败限流** —— 教师账号是唯一能开课、回收全班实例、导出全班数据库密码的入口。
   此前 /api/auth/login 与 /api/student/login 都没有任何失败计数，可以无限次试密码。
2. **安全响应头** —— 此前 app 一个中间件都没有。教师端页面里带 JWT，被 iframe 嵌套
   就能做点击劫持；响应体带全班凭据，被中间缓存就是数据泄漏。
3. **请求耗时日志** —— 只有 /healthz 存活探针，学生说"很卡"时无从定位。
   只记 method/path/status/耗时，**不记请求体**——避免把密码写进日志。

设计取舍（接手维护需知）：
- **限流状态放进程内存，不引 Redis**：单进程 systemd 部署，多一个依赖就多一个故障点。
  代价是重启后计数清零——可接受，重启本身就是运维动作，且攻击者无法触发重启。
- **只对失败计数，成功即清零**：教师一节课连续登录三次不会被误锁。
- **键取 (IP, 账号) 而不是只取 IP**：机房出口 NAT 后整班同一 IP，
  只按 IP 限流会把全班一起锁死；按账号 + IP 组合才既精准又不误伤。
- **状态表有容量上限 + LRU 淘汰**：否则攻击者伪造十万个学号就能把内存撑爆，
  限流器自己变成 DoS 入口。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

from . import config

log = logging.getLogger("mysql-lab.security")


# ---------------------------------------------------------------------------
# 一、登录失败限流
# ---------------------------------------------------------------------------
class FailLimiter:
    """固定窗口失败计数 + 锁定。线程安全，带容量上限防内存耗尽。

    时间基准用 time.monotonic()：系统时间被 NTP 校准时不会导致锁提前失效。
    """

    def __init__(self, max_fails: int, lock_seconds: int, window_seconds: int,
                 max_keys: int):
        self.max_fails = max(1, max_fails)
        self.lock_seconds = max(1, lock_seconds)
        self.window_seconds = max(1, window_seconds)
        self.max_keys = max(100, max_keys)
        # key -> [失败次数, 首次失败时刻, 锁定到期时刻(0=未锁)]
        self._state: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """已锁定则抛 429（带 Retry-After），否则放行。"""
        now = time.monotonic()
        remaining = 0
        with self._lock:
            rec = self._state.get(key)
            if rec is None:
                return
            locked_until = rec[2]
            if locked_until > now:
                remaining = int(locked_until - now) + 1
            elif locked_until and locked_until <= now:
                self._state.pop(key, None)   # 锁过期，整条记录作废
                return
            elif now - rec[1] > self.window_seconds:
                self._state.pop(key, None)   # 计数窗口过期
                return
        if remaining:
            raise HTTPException(
                status_code=429,
                detail=f"登录尝试次数过多，请 {remaining} 秒后再试",
                headers={"Retry-After": str(remaining)},
            )

    def fail(self, key: str) -> int:
        """记一次失败，返回剩余可试次数（0 表示本次已触发锁定）。"""
        now = time.monotonic()
        with self._lock:
            rec = self._state.get(key)
            if rec is None or now - rec[1] > self.window_seconds:
                rec = [0.0, now, 0.0]
            rec[0] += 1
            if rec[0] >= self.max_fails:
                rec[2] = now + self.lock_seconds
            self._state[key] = rec
            self._state.move_to_end(key)
            while len(self._state) > self.max_keys:
                self._state.popitem(last=False)   # 淘汰最久未更新的键
            return max(0, self.max_fails - int(rec[0]))

    def reset(self, key: str) -> None:
        """登录成功 → 清零，避免正常用户被历史失败次数连累。"""
        with self._lock:
            self._state.pop(key, None)

    def stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {
                "tracked": len(self._state),
                "locked": sum(1 for r in self._state.values() if r[2] > now),
                "max_fails": self.max_fails,
                "lock_seconds": self.lock_seconds,
                "max_tracked": self.max_keys,
            }


login_limiter = FailLimiter(
    max_fails=config.LOGIN_MAX_FAILS,
    lock_seconds=config.LOGIN_LOCK_SECONDS,
    window_seconds=config.LOGIN_FAIL_WINDOW,
    max_keys=config.LOGIN_MAX_TRACKED,
)


def client_ip(request: Request) -> str:
    """取客户端真实 IP。

    TRUST_PROXY=false 时只认 TCP 对端地址——直连部署下 X-Forwarded-For 可被任意伪造，
    信任它等于给攻击者一个"每次换 IP"的开关，限流形同虚设。
    """
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_key(scope: str, request: Request, account: str) -> str:
    """限流键：作用域 + IP + 账号。账号转小写，避免大小写变体绕过计数。"""
    return f"{scope}|{client_ip(request)}|{account.strip().lower()}"


# ---------------------------------------------------------------------------
# 二、安全响应头
# ---------------------------------------------------------------------------
# CSP 说明：前端是原生 HTML + 内联 <script>/<style>，没有构建链也就没有 nonce，
# 因此必须放开 'unsafe-inline'，否则两个页面直接白屏。这比"完全不开 CSP"仍有价值：
# 它挡住了外域脚本加载（script-src 'self'）与表单外发（form-action 'self'）。
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",          # 禁止 MIME 嗅探，防止把 JSON 当 HTML 执行
    "X-Frame-Options": "DENY",                    # 防点击劫持：教师端带 JWT，绝不能被嵌套
    "Referrer-Policy": "no-referrer",             # 别把带 lab_id 的 URL 泄给外站
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
}

# 健康探针一秒被探几十次，不记日志，否则真日志被刷没
_NO_LOG_PATHS = {"/healthz"}


async def security_middleware(request: Request, call_next):
    """统一出口：加安全头 + 记耗时。注意本函数是 async，业务路由是 sync def，
    FastAPI 会自动把 sync 路由丢线程池，不会阻塞事件循环。"""
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # 未捕获异常会由外层 ServerErrorMiddleware 变成裸 500，不走下面加头的逻辑；
        # 先把耗时和路径记下来，否则 500 在日志里只剩一段 traceback，不知道是哪个请求
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log.exception("未捕获异常 %s %s（%.0fms）",
                      request.method, request.url.path, elapsed_ms)
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)

    # API 响应一律不缓存：凭据 CSV、学生练习记录、审计日志都不能落到中间缓存
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")

    # HSTS 只在确认走 HTTPS 时下发。明文端口发 HSTS 会被浏览器忽略，
    # 但如果哪天前置了 Nginx + 证书，这一行就自动生效，不用回来改代码。
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    path = request.url.path
    if path not in _NO_LOG_PATHS:
        level = logging.WARNING if elapsed_ms >= config.SLOW_REQUEST_MS else logging.INFO
        log.log(level, "%s %s -> %s %.0fms", request.method, path,
                response.status_code, elapsed_ms)
    return response
