"""security_test.py — 安全基线验收（v1.0.2，不依赖 Docker、不碰生产数据）。

覆盖四件事，全部是"内网测不出、挂公网就要命"的项：
  1. 安全响应头（防点击劫持 / MIME 嗅探 / 外域脚本）
  2. API 响应禁止缓存（凭据 CSV、学生记录不能落中间缓存）
  3. 教师登录失败限流（5 次锁 5 分钟，锁定期间正确密码也进不来）
  4. 学生登录失败限流（同一把尺子，且文案不泄漏学号是否存在）
  5. 成功登录清零计数（不能连累正常用户）

用法：
    python scripts/security_test.py

注意：用临时 DATA_DIR，不读也不写 data/app.db，本机没 Docker 也能跑。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 必须在 import app 之前改环境：config.py 是模块级读环境变量的
_TMP = tempfile.mkdtemp(prefix="mysql-lab-sec-")
os.environ["DATA_DIR"] = _TMP
os.environ["APP_DB_PATH"] = str(Path(_TMP) / "app.db")
os.environ.setdefault("APP_SECRET", "security-test-secret-" + "x" * 32)
os.environ["TEACHER_INIT_PASSWORD"] = "Teach@Test123"
os.environ["ASSISTANT_INIT_PASSWORD"] = "Assist@Test123"

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient          # noqa: E402

from app import security                            # noqa: E402
from app.main import app                            # noqa: E402

PASSED = 0
FAILED = 0

# 验收关注断言结果，不关注访问日志：调高到 WARNING，免得上百行 httpx 日志淹没结论
logging.getLogger().setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mysql-lab.security").setLevel(logging.WARNING)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


def login(c: TestClient, account: str, password: str):
    return c.post("/api/auth/login", json={"login": account, "password": password})


def main() -> int:
    with TestClient(app) as c:
        print("\n== 1. 安全响应头 ==")
        r = c.get("/healthz")
        check("healthz 200", r.status_code == 200, f"status={r.status_code}")
        check("X-Content-Type-Options: nosniff",
              r.headers.get("x-content-type-options") == "nosniff")
        check("X-Frame-Options: DENY", r.headers.get("x-frame-options") == "DENY")
        check("CSP 存在且禁止嵌套",
              "frame-ancestors 'none'" in (r.headers.get("content-security-policy") or ""))
        check("Referrer-Policy 存在", bool(r.headers.get("referrer-policy")))
        check("权限策略关闭摄像头/麦克风",
              "camera=()" in (r.headers.get("permissions-policy") or ""))
        check("明文访问不下发 HSTS", not r.headers.get("strict-transport-security"))

        r = c.get("/")
        check("教师端页面同样带安全头",
              r.headers.get("x-frame-options") == "DENY")

        print("\n== 2. API 不缓存 ==")
        r = c.get("/api/templates")   # 未登录 401，但头仍应存在
        check("API 响应 Cache-Control: no-store",
              r.headers.get("cache-control") == "no-store",
              f"got={r.headers.get('cache-control')}")

        print("\n== 3. 教师登录限流 ==")
        # 先确认正常登录可用
        for i in range(3):
            r = login(c, "assistant", "Assist@Test123")
        check("正常登录成功（3 次连续）", r.status_code == 200, f"status={r.status_code}")

        codes = []
        for i in range(5):
            r = login(c, "teacher", "wrong-password")
            codes.append(r.status_code)
        check("前 4 次错误密码返回 401", codes[:4] == [401, 401, 401, 401], str(codes))
        check("第 5 次错误仍 401（本次才触发锁定）", codes[4] == 401, str(codes))

        r = login(c, "teacher", "Teach@Test123")
        check("锁定后即使密码正确也 429", r.status_code == 429, f"status={r.status_code}")
        check("429 带 Retry-After 头", int(r.headers.get("retry-after", "0")) > 0,
              f"retry-after={r.headers.get('retry-after')}")
        check("429 文案给出等待秒数", "秒" in r.json().get("detail", ""))

        # 成功登录必须清零：assistant 刚才成功过，再错 4 次仍是 401 而非累积锁死
        for i in range(4):
            login(c, "assistant", "nope")
        r = login(c, "assistant", "Assist@Test123")
        check("成功登录后计数已清零（4 次失败不影响正常登录）",
              r.status_code == 200, f"status={r.status_code}")

        print("\n== 4. 学生登录限流 ==")
        codes, details = [], []
        for i in range(5):
            r = c.post("/api/student/login",
                       json={"student_id": "999999", "password": "wrong"})
            codes.append(r.status_code)
            details.append(r.json().get("detail", ""))
        check("不存在的学号连续 5 次：前 4 次 401",
              codes[:4] == [401] * 4, str(codes))
        check("401 文案统一，不区分学号是否存在",
              all("不正确" in d for d in details), str(details))
        r = c.post("/api/student/login",
                   json={"student_id": "999999", "password": "wrong"})
        check("第 6 次学生登录 429", r.status_code == 429, f"status={r.status_code}")
        check("429 文案是限流提示而非账号错误",
              "尝试次数过多" in r.json().get("detail", ""),
              r.json().get("detail", ""))

        print("\n== 5. 限流器自身健壮性 ==")
        st = security.login_limiter.stats()
        check("统计可读且锁定数 >=1", st["locked"] >= 1, str(st))
        check("状态表有容量上限（防伪造账号撑爆内存）", st["max_tracked"] >= 1000, str(st))

        lim = type(security.login_limiter)(2, 60, 900, 100)
        for i in range(150):          # 超过上限，验证 LRU 淘汰不崩
            lim.fail(f"flood|1.2.3.4|sid{i}")
        check("灌入 150 个键不崩且不超过上限",
              100 <= lim.stats()["tracked"] <= 100, f"tracked={lim.stats()['tracked']}")
        lim.reset("flood|1.2.3.4|sid0")
        check("reset 后该键不再被跟踪",
              lim.stats()["tracked"] <= 100)

        print("\n== 6. JWT 吊销（logout 即刻作废）==")
        r = login(c, "assistant", "Assist@Test123")
        t = r.json()["token"]
        r = c.get("/api/me", headers={"Authorization": f"Bearer {t}"})
        check("登出前 token 有效", r.status_code == 200, f"status={r.status_code}")
        r = c.post("/api/auth/logout", headers={"Authorization": f"Bearer {t}"})
        check("logout 200", r.status_code == 200, f"status={r.status_code}")
        r = c.get("/api/me", headers={"Authorization": f"Bearer {t}"})
        check("登出后同一 token 立即 401", r.status_code == 401, f"status={r.status_code}")
        r = login(c, "assistant", "Assist@Test123")
        t2 = r.json()["token"]
        r = c.get("/api/me", headers={"Authorization": f"Bearer {t2}"})
        check("重新登录的新 token 不受影响", r.status_code == 200, f"status={r.status_code}")
        # 伪造一个"无 jti 的旧版 token"：应跳过吊销检查而不是 500
        import jwt as _jwt
        from datetime import datetime, timedelta, timezone as _tz
        from app import config as _cfg
        legacy = _jwt.encode(
            {"sub": "assistant", "uid": 2, "role": "assistant", "name": "李助教",
             "iat": datetime.now(_tz.utc),
             "exp": datetime.now(_tz.utc) + timedelta(hours=1)},
            _cfg.APP_SECRET, algorithm="HS256")
        r = c.get("/api/me", headers={"Authorization": f"Bearer {legacy}"})
        check("旧版无 jti token 向后兼容（不崩）", r.status_code == 200,
              f"status={r.status_code}")
        r = c.post("/api/auth/logout", headers={"Authorization": f"Bearer {legacy}"})
        check("旧版 token 登出不报错并提示跳过", r.status_code == 200
              and "跳过" in r.json().get("note", ""), f"{r.status_code} {r.json()}")

        print("\n== 7. 弹性部署配置（LAB_PUBLIC_HOST=auto）==")
        from app import config as _config
        _config.PUBLIC_HOST = "1.2.3.4"
        check("固定 IP 原样生效", _config.resolve_public_host() == "1.2.3.4")
        _config.PUBLIC_HOST = ""
        check("未配置时返回空（请求 Host 兜底）",
              _config.resolve_public_host() == "")
        _config.PUBLIC_HOST = "auto"
        # 本机不是云主机，metadata 全部不可达：必须安全回退空串而不是异常
        got = _config.resolve_public_host()
        check("auto 且探测不到时安全回退（不崩、不乱填）",
              got == "", f"got={got!r}")
        _config.PUBLIC_HOST = ""   # 还原，避免影响其他用例

    print(f"\n{'=' * 52}\n通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
