"""任务 3 验收脚本：models.py 建表 + 登录鉴权（在服务器上运行）。

用法: python3.11 scripts/task3_test.py
验收标准（交接文档第八节任务 3）:
  教师能登录拿到 JWT，学生角色访问教师接口被拒 403。
额外覆盖: 无凭证 401 / 坏密码 401 / 学生查自己身份 / 审计留痕 / 学生密码加密存储。
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8100

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def req(method: str, path: str, token: str | None = None,
        body: dict | None = None) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="task3_")
    os.environ["APP_DB_PATH"] = os.path.join(tmpdir, "app.db")
    os.environ["APP_SECRET"] = "task3-test-secret-" + "0" * 32

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            try:
                code, _ = req("GET", "/healthz")
                if code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("服务未启动")
            return 1

        # ---- 1. 教师登录拿 JWT（验收标准①）----
        code, body = req("POST", "/api/auth/login",
                         body={"login": "teacher", "password": "<演示用默认密码，部署时须修改>"})
        check("教师登录 200", code == 200, f"code={code}")
        check("响应含 token/role/name",
              all(k in body for k in ("token", "role", "name")) and body.get("role") == "teacher",
              str(body)[:80])
        teacher_token = body.get("token", "")
        check("JWT 是三段式", teacher_token.count(".") == 2)

        # ---- 2. 教师访问教师接口 ----
        code, body = req("GET", "/api/templates", token=teacher_token)
        check("教师访问 /api/templates 200", code == 200, f"code={code}")
        check("返回样例模板", code == 200 and any(t["name"] == "基础三表实验" for t in body))

        # ---- 3. 学生角色访问教师接口被拒 403（验收标准②）----
        code, body = req("POST", "/api/auth/login",
                         body={"login": "240101", "password": "Stu@2026"})
        check("学生登录 200", code == 200)
        student_token = body.get("token", "")
        code, body = req("GET", "/api/templates", token=student_token)
        check("学生访问教师接口 403", code == 403, f"code={code} body={body}")

        # ---- 4. 边界：无凭证 401 / 坏密码 401 / 过期或伪造 token 401 ----
        code, _ = req("GET", "/api/templates")
        check("无凭证 401", code == 401, f"code={code}")
        code, _ = req("POST", "/api/auth/login",
                      body={"login": "teacher", "password": "wrong"})
        check("坏密码 401", code == 401, f"code={code}")
        code, _ = req("GET", "/api/templates", token="forged.token.here")
        check("伪造 token 401", code == 401, f"code={code}")
        code, body = req("GET", "/api/me", token=student_token)
        check("学生查 /api/me 可见自己角色", code == 200 and body.get("role") == "student")

        # ---- 5. 审计留痕 + 学生密码非明文 ----
        dbf = os.path.join(tmpdir, "app.db")
        conn = sqlite3.connect(dbf)
        n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='login'").fetchone()[0]
        check("登录写审计日志", n >= 2, f"rows={n}")
        row = conn.execute(
            "SELECT password_hash FROM users WHERE login='240101'").fetchone()
        check("学生密码非明文(PBKDF2)", row and row[0].startswith("pbkdf2$") and "Stu@2026" not in row[0])
        conn.close()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + ("任务 3 全部通过 ✓" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
