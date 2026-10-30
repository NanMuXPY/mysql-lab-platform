"""insight_demo.py — P1 错题诊断验收（服务器上运行）。

做三件事：
1. 给指定实验单的容器开启 general_log（新建容器默认已开；老容器运行时补开）
2. 以学生账号真实执行几条"典型失误" SQL（语法错/无 WHERE/缺 JOIN 条件/SELECT *）
3. 调用 insights.lab_errors 采集 + 诊断，打印教师端将看到的结果

运行: python3.11 scripts/insight_demo.py [lab_id]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import crypto, db, insights, models  # noqa: E402

MISTAKES = [
    # 语法错误：SELECT 拼错（注意：MySQL 不把 1064 写进 general log，抓不到）
    "SELEC ename FROM emp;",
    # 表名拼错：语法合法但执行必失败（ERROR 1146，能抓到）
    "SELECT empno FROM empp;",
    # 危险操作：无 WHERE 的 UPDATE（演示里用事务回滚，不破坏数据）
    "BEGIN; UPDATE emp SET salary = 0; ROLLBACK;",
    # 写法提示：SELECT *（带前缀，避免被当作噪声）
    "SELECT * FROM emp;",
    # 正确示范
    "SELECT ename, salary FROM emp WHERE deptno = 10 ORDER BY salary DESC;",
]


def sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def enable_general_log(container: str) -> None:
    root_pw = sh(f"docker exec {container} printenv MYSQL_ROOT_PASSWORD").strip()
    if not root_pw:
        print(f"    {container}: 取不到 root 密码，跳过")
        return
    sql = ("SET GLOBAL log_output='FILE'; "
           "SET GLOBAL general_log_file='/var/lib/mysql/general.log'; "
           "SET GLOBAL general_log=ON;")
    out = sh(f"docker exec {container} mysql -h127.0.0.1 -uroot -p{root_pw} -e \"{sql}\"")
    print(f"    {container}: general_log 已开{'（' + out[:60] + '）' if out else ''}")


def main() -> None:
    insights.init()
    lab_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = db.connect()
    try:
        if lab_id is None:
            row = conn.execute(
                "SELECT lab_id FROM instances WHERE status='running' "
                "GROUP BY lab_id ORDER BY lab_id DESC LIMIT 1").fetchone()
            if not row:
                print("没有运行中的实验单")
                return
            lab_id = row["lab_id"]
        instances = conn.execute(
            "SELECT student_id, container_name, db_name, db_user, db_pass_enc "
            "FROM instances WHERE lab_id = ? AND status='running'", (lab_id,)
        ).fetchall()
    finally:
        conn.close()

    print(f"== 实验单 {lab_id}：{len(instances)} 个实例 ==")
    for inst in instances:
        c = inst["container_name"]
        enable_general_log(c)
        pw = crypto.decrypt(inst["db_pass_enc"])
        for sql in MISTAKES:
            sh(f'docker exec {c} mysql -h127.0.0.1 -u{inst["db_user"]} -p{pw} '
               f'{inst["db_name"]} -e "{sql}" 2>/dev/null')
        print(f"    {c}: 已模拟 {len(MISTAKES)} 条操作（含 3 条典型失误）")

    print("\n== 诊断结果（教师端错题诊断页看到的内容）==")
    d = insights.lab_errors(lab_id)
    print(f"本轮采集: {d['collected']}")
    for s in d["students"]:
        print(f"\n  学号 {s['student_id']}  练习 {s['total']} 条 / 正确 {s['ok']} / 问题 {s['problems']}")
        for x in s["samples"]:
            flag = {"syntax_error": "语法错误", "dangerous": "危险操作",
                    "hint": "写法提示"}.get(x["verdict"], x["verdict"])
            print(f"    [{flag}] {x['sql'][:60]}  ← {x['detail']}")


if __name__ == "__main__":
    main()
