"""stress_test.py — 任务 7：批量交付压测（交接文档第八节第 7 项）。

压出写进项目简介的三个数字：总耗时、内存占用、单实例均值。

做法（全部走平台真实 API，不走捷径直连 Docker）：
  1. 登录教师账号，创建一个 N 人的实验单（并发 5 的真实交付路径）
  2. 每 2 秒轮询进度，记录每个实例首次 running 的时间 → 交付曲线
  3. 全部终态后：docker stats 采内存、free 采主机、
     逐容器 docker exec 真实登录（SELECT 1）测延迟
  4. 生成 Markdown 报告 docs/压测报告-<时间>.md
  5. 默认回收实验单（--keep 保留）

用法（在服务器上跑）：
  python3.11 scripts/stress_test.py --n 20
  python3.11 scripts/stress_test.py --n 10 --keep
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import urllib.request  # noqa: E402

BASE = "http://127.0.0.1:8000"
TEACHER = "teacher"
PASSWORD = "<演示用默认密码，部署时须修改>"   # 种子教师密码；正式部署改密后用 --user/--pass 覆盖


def api(path: str, method: str = "GET", body: dict | None = None,
        token: str = "") -> dict:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True, timeout=120).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--keep", action="store_true", help="不回收，保留容器供观察")
    args = ap.parse_args()
    n = args.n

    print(f"[1/5] 登录并创建 {n} 人实验单…")
    token = api("/api/auth/login", "POST",
                {"login": TEACHER, "password": PASSWORD})["token"]
    tpls = api("/api/templates", token=token)
    tpl_id = tpls[0]["id"]
    students = [f"8{i:05d}" for i in range(1, n + 1)]
    t0 = time.time()
    lab = api("/api/labs", "POST", {
        "template_id": tpl_id, "name": f"压测-{n}实例",
        "class_name": "压测", "duration_min": 120, "student_ids": students,
    }, token)
    lab_id = lab["lab_id"]
    print(f"      实验单 #{lab_id} 已受理")

    print("[2/5] 轮询交付进度…")
    first_running: dict[str, float] = {}   # student_id -> 首次 running 时刻
    terminal, failed = 0, 0
    timeline = []
    while time.time() - t0 < 900:          # 15 分钟兜底
        time.sleep(2)
        p = api(f"/api/labs/{lab_id}", token=token)
        c = p["summary"]                    # 形如 {"running": 5, "creating": 3}
        for inst in p["instances"]:
            if inst["status"] == "running" and inst["student_id"] not in first_running:
                first_running[inst["student_id"]] = time.time() - t0
        n_run, n_fail = c.get("running", 0), c.get("failed", 0)
        if n_run > terminal or n_fail > failed:
            print(f"      {time.time()-t0:6.1f}s  running={n_run} failed={n_fail}")
        terminal, failed = n_run, n_fail
        timeline.append((round(time.time() - t0, 1), n_run, n_fail))
        if n_run + n_fail >= n:
            break
    total_create = time.time() - t0
    print(f"      交付完成：{total_create:.1f}s")

    print("[3/5] 采集内存与真实登录延迟…")
    stats_raw = sh("docker stats --no-stream --format "
                   "'{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}' | grep lab-")
    containers = []
    for line in stats_raw.splitlines():
        name, mem, cpu = line.split("|")
        containers.append((name, mem, cpu))
    free_out = sh("free -m")

    # 真实登录：进容器用学生账号 SELECT 1（mysql 协议完整握手）
    # 密码从元数据库解密（脚本就跑在服务器上，直接用平台自己的加密层）
    from app import crypto, db as appdb
    conn = appdb.connect()
    try:
        rows = conn.execute(
            "SELECT student_id, db_pass_enc FROM instances WHERE lab_id = ?",
            (lab_id,)).fetchall()
    finally:
        conn.close()
    passwords = {r["student_id"]: crypto.decrypt(r["db_pass_enc"]) for r in rows}
    logins = []
    for name, _, _ in containers:
        sid = name.rsplit("-", 1)[1].replace("stu_", "")
        t = time.time()
        out = sh(f"docker exec {name} mysql -h127.0.0.1 -ustu_{sid} "
                 f"-p{passwords.get(sid, '')} -e 'SELECT 1' 2>/dev/null | head -1")
        ok = "1" in out
        logins.append(ok)
    login_ok = sum(logins)
    print(f"      真实登录 {login_ok}/{len(containers)} 成功")

    print("[4/5] 生成报告…")
    create_times = sorted(first_running.values())
    avg_create = sum(create_times) / len(create_times) if create_times else 0
    mem_values = []
    for _, mem, _ in containers:
        try:
            mem_values.append(float(mem.split("MiB")[0]))
        except (ValueError, IndexError):
            pass
    avg_mem = sum(mem_values) / len(mem_values) if mem_values else 0

    lines = [
        f"# 压测报告：{n} 实例批量交付",
        f"\n- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 服务器：{sh('nproc')} 核 / {free_out.splitlines()[1].split()[1]} MB 内存",
        f"- 容器限额：0.5 核 CPU / 256MB 内存（config.LAB_MEM_LIMIT）",
        f"- 实验单：#{lab_id}（模板 {tpls[0]['name']}，并发 5）",
        "",
        "## 结果",
        f"| 指标 | 数值 |",
        f"|---|---|",
        f"| 交付总耗时 | **{total_create:.1f}s** |",
        f"| 成功 / 失败 | {len(create_times)} / {n - len(create_times)} |",
        f"| 单实例平均就绪时间 | **{avg_create:.1f}s** |",
        f"| 首个实例就绪 | {create_times[0]:.1f}s |" if create_times else "",
        f"| 尾个实例就绪 | {create_times[-1]:.1f}s |" if create_times else "",
        f"| 单容器实际内存 | **平均 {avg_mem:.0f} MiB**（限额 256MiB） |",
        f"| 全部容器内存合计 | {sum(mem_values):.0f} MiB |",
        f"| 真实登录（SELECT 1） | {login_ok}/{len(containers)} |",
        "",
        "## 主机内存（压测后）",
        "```", free_out, "```",
        "",
        "## 交付曲线（每 2 秒采样）",
        "| 时刻(s) | running | failed |",
        "|---|---|---|",
    ]
    lines += [f"| {t} | {r} | {f} |" for t, r, f in timeline[::5]]  # 隔 10s 采样一条
    report = "\n".join(x for x in lines if x != "") + "\n"
    report_path = ROOT / "docs" / f"压测报告-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report_path)

    if not args.keep:
        print("[5/5] 回收实验单，归还资源…")
        r = api(f"/api/labs/{lab_id}/recycle", "POST", token=token)
        print(f"      已回收 {r['recycled']} 个实例")
    else:
        print(f"[5/5] --keep：实验单 #{lab_id} 保留（记得手动回收）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
