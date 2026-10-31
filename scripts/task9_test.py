"""task9_test.py — 任务 9 验收：练习报告 CSV / 学生自查页 / 鉴权与越权。

覆盖三块新增能力：
  1. 教师端「导出练习报告 CSV」—— 一人一行，UTF-8 BOM，可被 Excel 直接打开
  2. 学生端「我的练习」/api/student/my —— 学号 + 数据库密码登录，只看得到自己
  3. 鉴权边界 —— 教师 token 不能当学生用、学生 token 不能碰教师接口

用法：python scripts/task9_test.py [base_url]
      默认 http://127.0.0.1:8000（服务器本机）；本机验远程传 http://<服务器公网IP>:8000
依赖：仅标准库 urllib，不需要 requests
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("LAB_BASE", "http://127.0.0.1:8000")).rstrip("/")
TEACHER = os.environ.get("LAB_TEACHER", "teacher")
TEACHER_PW = os.environ.get("LAB_TEACHER_PW", "<演示用默认密码，部署时须修改>")

PASS = FAIL = 0


def req(path: str, data=None, tok: str | None = None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(
        BASE + path, data=json.dumps(data).encode() if data else None, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # 连接失败也要给出可读结论，而不是抛栈
        return 0, str(e).encode()


def js(b: bytes):
    try:
        return json.loads(b)
    except Exception:
        return {}


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def main() -> int:
    print(f"目标：{BASE}\n")

    # ---- 1. 教师登录 ----
    s, b = req("/api/auth/login", {"login": TEACHER, "password": TEACHER_PW})
    T = js(b).get("token", "")
    check("教师登录", s == 200 and bool(T), f"status={s}")
    if not T:
        print("\n教师登录失败，后续用例无法进行。")
        return 1

    # ---- 2. 找一个有活跃实例的实验单 ----
    s, b = req("/api/labs", tok=T)
    labs = js(b) if s == 200 else []
    target = None
    for l in labs:
        if l["status"] != "running":
            continue
        s2, b2 = req(f"/api/labs/{l['id']}/errors", tok=T)
        d2 = js(b2) if s2 == 200 else {}
        if d2.get("students"):
            target = l
            break
    check("找到有练习记录的运行中实验单", target is not None,
          f"lab={target['id'] if target else None}")
    if not target:
        print("\n没有可用于学生侧验证的实验单（需 running 且有 SQL 记录）。")
        print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
        return 1
    lab_id = target["id"]

    # ---- 3. 练习报告 CSV ----
    s, b = req(f"/api/labs/{lab_id}/report.csv", tok=T)
    check("练习报告导出 200", s == 200, f"status={s}")
    check("报告带 UTF-8 BOM（Excel 不乱码）", b[:3] == b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(b.decode("utf-8-sig", "replace"))))
    check("报告有数据行", len(rows) > 0, f"{len(rows)} 行")
    need = ["学号", "练习总数", "正确率(%)", "语法错误", "表不存在",
            "列不存在", "危险操作", "最近一条问题命令", "诊断"]
    check("报告表头字段齐全", all(k in (rows[0] if rows else {}) for k in need),
          f"缺 {[k for k in need if rows and k not in rows[0]]}")
    ok_acc = all(0 <= float(r["正确率(%)"]) <= 100 for r in rows if r["练习总数"] != "0")
    check("正确率落在 0-100", ok_acc)

    # ---- 4. 取一个学生的真实密码（走连接信息 CSV，模拟教师分发）----
    s, b = req(f"/api/labs/{lab_id}/export", tok=T)
    check("连接信息导出 200", s == 200, f"status={s}")
    creds = list(csv.DictReader(io.StringIO(b.decode("utf-8-sig", "replace"))))
    check("连接信息非空", len(creds) > 0, f"{len(creds)} 人")
    if not creds:
        print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
        return 1
    stu = creds[0]

    # ---- 5. 学生端登录与自查 ----
    s, b = req("/api/student/login",
               {"student_id": stu["student_id"], "password": stu["password"]})
    d = js(b)
    ST = d.get("token", "")
    check("学生用学号+数据库密码登录", s == 200 and bool(ST), f"status={s}")
    check("返回实验单信息", d.get("lab_id") == lab_id and bool(d.get("lab_name")))

    s, b = req("/api/student/my", tok=ST)
    my = js(b)
    check("我的练习 200", s == 200, f"status={s}")
    check("学号与登录者一致", my.get("student_id") == stu["student_id"])
    check("连接信息完整（主机/端口/库名/用户）",
          all(my.get("conn", {}).get(k) for k in ("host", "port", "database", "user")),
          str(my.get("conn")))
    check("统计总数 > 0", (my.get("total") or 0) > 0, f"total={my.get('total')}")
    check("命令回放非空", bool(my.get("timeline")), f"{len(my.get('timeline', []))} 条")
    check("回放按时间正序",
          [x["t"] for x in my.get("timeline", [])]
          == sorted(x["t"] for x in my.get("timeline", [])))
    check("每条回放都有判定与命令原文",
          all(x.get("verdict") and x.get("sql") for x in my.get("timeline", [])))

    # ---- 6. 鉴权边界 ----
    s, _ = req("/api/student/login",
               {"student_id": stu["student_id"], "password": "definitely-wrong"})
    check("错误密码被拒 401", s == 401, f"status={s}")
    s, _ = req("/api/student/login", {"student_id": "000000", "password": "x"})
    check("不存在的学号被拒 401", s == 401, f"status={s}")
    s, _ = req("/api/student/my", tok=T)
    check("教师 token 当学生用被拒 403", s == 403, f"status={s}")
    s, _ = req("/api/student/my")
    check("无 token 访问学生接口 401", s == 401, f"status={s}")
    s, _ = req("/api/labs", tok=ST)
    check("学生 token 访问教师接口 403", s == 403, f"status={s}")
    s, _ = req(f"/api/labs/{lab_id}/report.csv", tok=ST)
    check("学生 token 导出班级报告 403", s == 403, f"status={s}")

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
