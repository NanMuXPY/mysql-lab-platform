"""模拟学生在实例里敲一串 SQL（命令回放功能的演示/回归用，一次性脚本）。

用法：python3.11 scripts/simulate_student.py [lab_id] [student_id]
故意混入正确语句 + 各类典型失误，用来验证教师端"每人一张命令回放表"的展示效果。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import crypto, db  # noqa: E402

LAB = int(sys.argv[1]) if len(sys.argv) > 1 else 20
STUDENT = sys.argv[2] if len(sys.argv) > 2 else "550001"
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 0  # >0 时只发前 N 条（造"全对"学生对比用）

conn = db.connect()
try:
    inst = conn.execute(
        "SELECT container_name, db_name, db_user, db_pass_enc FROM instances "
        "WHERE lab_id = ? AND student_id = ?", (LAB, STUDENT)
    ).fetchone()
finally:
    conn.close()
if not inst:
    sys.exit(f"实验单 {LAB} 没有学生 {STUDENT} 的实例")

pwd = crypto.decrypt(inst["db_pass_enc"])

SQLS = [
    "SELECT * FROM customer;",                                   # 写法提示：SELECT *
    "SELECT cname, city FROM customer WHERE city = '深圳';",      # 完全正确
    "SELECT c.cname, o.oid FROM customer c JOIN orders o;",        # 教学坑：JOIN 缺 ON
    "SELECT * FROM customerr;",                                   # 教学坑：表名拼错
    "SELECT * FROM orders WHERE amount > 100;",                    # 教学坑：列名不存在
    "UPDATE product SET price = 0;",                               # 教学坑：UPDATE 无 WHERE
    "DELETE FROM orders;",                                         # 教学坑：DELETE 无 WHERE
    "SELECT o.oid, c.cname, p.pname FROM orders o "
    "JOIN customer c ON o.cid = c.cid "
    "JOIN product p ON o.pid = p.pid;",                            # 完全正确的三表连接
]

if LIMIT > 0:
    SQLS = SQLS[:LIMIT]

for s in SQLS:
    r = subprocess.run(
        ["docker", "exec", inst["container_name"], "mysql", "-h127.0.0.1",
         f"-u{inst['db_user']}", f"-p{pwd}", "-D", inst["db_name"], "-e", s],
        capture_output=True, text=True,
    )
    tag = "OK " if r.returncode == 0 else "ERR"
    print(f"[{tag}] {s}")
    if r.returncode != 0:
        print("      ", (r.stderr or "").splitlines()[-1][:120])
    time.sleep(0.5)  # 让 general_log 时间戳错开，回放表里能看出先后顺序

print(f"完成：向 lab {LAB} / {STUDENT} 发送 {len(SQLS)} 条 SQL")
