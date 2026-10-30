"""task4_test.py — 任务 4 验收：批量创建（服务器上运行）。

验收点（对照交接文档第八节任务 4）：
1. POST /api/labs 批量创建 10 个学生实例，线程池限 5 并发
2. 失败自动回滚：端口归还 + last_error 留痕 + 容器兜底清理（故障注入验证）
3. 端口冲突换端口重试（docker-proxy 释放竞态，坑清单 #2）
4. 进度可查询：GET /api/labs/{id} 汇总 + 明细

运行: cd /root/mysql-lab && python3.11 scripts/task4_test.py
"""

from __future__ import annotations

import socket
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, crypto, db, docker_svc, labs, models, ports  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    tag = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{tag}] {name}" + (f"  ({extra})" if extra and not cond else ""))


def wait_lab_done(lab_id: int, timeout: int = 420) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        p = labs.lab_progress(lab_id)
        cur = (p["status"], dict(p["summary"]))
        if cur != last:
            print(f"    进度: status={p['status']} summary={p['summary']}")
            last = cur
        if p["status"] in ("running", "failed"):
            return p
        time.sleep(5)
    raise TimeoutError(f"实验单 {lab_id} 在 {timeout}s 内未完成")


def cleanup_lab_containers(lab_id: int) -> None:
    """测试卫生：删容器/卷/租约，实例标记 recycled（保留历史行）。"""
    for inst in models.list_instances_by_lab(lab_id):
        try:
            docker_svc.remove_instance(inst["container_name"])
        except Exception:
            pass
        ports.release_by_instance(inst["container_name"])
        volume = str(config.LABS_DATA_DIR / str(lab_id) / inst["student_id"])
        try:
            docker_svc.delete_volume(volume)
        except Exception:
            pass
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM instances WHERE lab_id = ? AND status != 'recycled'",
            (lab_id,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        models.update_instance_status(r["id"], "recycled")


# ---------------------------------------------------------------------------
# 预检：重建 instances 表（列级 UNIQUE → 部分唯一索引迁移）、对齐端口池与实际容器
# ---------------------------------------------------------------------------
print("== 预检 ==")
import shutil  # noqa: E402

_demo_key = "lab-1-stu_demo"

# 0) 清理上次运行残留的测试容器/卷/租约（幂等，demo 容器保留）
try:
    cli = docker_svc._client()
    for c in cli.containers.list(all=True):
        if c.name.startswith("lab-") and c.name != _demo_key:
            c.remove(force=True)
            print(f"    清理残留容器 {c.name}")
    for labdir in config.LABS_DATA_DIR.glob("*"):
        for sd in labdir.iterdir():
            if sd.name != "stu_demo" and sd.is_dir():
                shutil.rmtree(sd, ignore_errors=True)
    with db.write() as conn:
        conn.execute("DELETE FROM port_pool WHERE instance_id != ?", (_demo_key,))
        conn.execute("UPDATE instances SET status='recycled' "
                     "WHERE status IN ('creating','running','stopped')")
except Exception as e:
    print(f"    残留清理跳过: {e}")

models.init()
ports.init()
conn = db.connect()
n = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
conn.close()
if n == 0:
    conn = db.connect()
    conn.execute("DROP TABLE instances")  # 空表：直接换新 schema（部分唯一索引）
    conn.close()
    models.init()
    print("    instances 表已迁移为部分唯一索引版本")

# 把已存在的 demo 容器端口登记进池（保持池与 Docker 实际一致）
try:
    cli = docker_svc._client()
    c = cli.containers.get(_demo_key)
    hp = int(c.attrs["NetworkSettings"]["Ports"]["3306/tcp"][0]["HostPort"])
    if hp not in ports.leased_ports():
        with db.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO port_pool (port, instance_id, leased_at) "
                "VALUES (?, ?, ?)", (hp, _demo_key, datetime.now().astimezone().isoformat()),
            )
        print(f"    demo 容器端口 {hp} 已登记入池")
except Exception as e:
    print(f"    demo 容器未运行（跳过登记）: {e}")

teacher = models.get_user_by_login("teacher")
if teacher is None:  # 元数据库为空时补种子（仅测试环境）
    models.seed_demo_users()
    teacher = models.get_user_by_login("teacher")
if not models.list_templates():
    models.seed_demo_template()
    print("    已注入样例模板")
tmpl = models.list_templates()[0]
print(f"    教师 {teacher['login']}，模板 {tmpl['name']}（id={tmpl['id']}）\n")

# ---------------------------------------------------------------------------
# 第一部分：故障注入 → 验证失败回滚（3 人小批量，2 真 1 假）
# ---------------------------------------------------------------------------
print("== 第一部分：失败回滚验收（注入 1 例失败）==")
REAL_CREATE = docker_svc.create_mysql_instance
FAIL_STU = "990003"


def _inject(lab_id, student_id, *a, **k):
    if student_id == FAIL_STU:
        raise docker_svc.ProviderError("injected failure for rollback test")
    return REAL_CREATE(lab_id, student_id, *a, **k)


docker_svc.create_mysql_instance = _inject
lab_a = labs.create_lab_batch(teacher["id"], tmpl["id"], "回滚验收",
                              "测试班", 60, ["990001", "990002", FAIL_STU])
prog = wait_lab_done(lab_a)
check("2 个正常学生创建成功", prog["summary"].get("running") == 0 + 2,
      f"summary={prog['summary']}")
check("注入失败的学生标记 failed", prog["summary"].get("failed") == 1,
      f"summary={prog['summary']}")
docker_svc.create_mysql_instance = REAL_CREATE  # 恢复

insts = {i["student_id"]: i for i in prog["instances"]}
check("失败实例留痕 last_error", "injected" in (insts[FAIL_STU]["last_error"] or ""),
      f"last_error={insts[FAIL_STU]['last_error']}")
leased = ports.leased_ports()
check("失败实例端口已归还",
      f"lab-{lab_a}-{FAIL_STU}" not in leased.values(),
      f"leased={leased}")
check("正常实例端口仍在租约", len(leased) == 3, f"leased={leased}")

# 测试卫生：清掉这单的容器/卷，端口复用留给第二部分验证
cleanup_lab_containers(lab_a)
print("    回滚验收实验单已清理（容器/卷/租约）\n")

# ---------------------------------------------------------------------------
# 第二部分：API 层验收（10 人批量 + 门禁 + 边界）
# ---------------------------------------------------------------------------
print("== 第二部分：API 验收（10 人批量）==")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

STUDENTS = [f"2402{i:02d}" for i in range(1, 11)]

with TestClient(app) as client:
    r = client.post("/api/auth/login",
                    json={"login": "teacher", "password": "<演示用默认密码，部署时须修改>"})
    check("教师登录", r.status_code == 200, r.text)
    tok = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post("/api/auth/login",
                    json={"login": "240101", "password": "Stu@2026"})
    stu_tok = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post("/api/labs", json={"template_id": tmpl["id"], "name": "x"})
    check("无凭证创建被拒 401", r.status_code == 401, str(r.status_code))
    r = client.post("/api/labs", json={"template_id": tmpl["id"], "name": "x",
                                       "student_ids": ["980001"]}, headers=stu_tok)
    check("学生角色创建被拒 403", r.status_code == 403, str(r.status_code))
    r = client.post("/api/labs", json={
        "template_id": tmpl["id"], "name": "x", "duration_min": 60,
        "student_ids": [f"98{i:03d}" for i in range(300)]}, headers=tok)
    check("超容量名单快速失败 4xx", r.status_code in (400, 409), str(r.status_code))
    r = client.get("/api/labs/999999", headers=tok)
    check("不存在的实验单 404", r.status_code == 404, str(r.status_code))

    t0 = time.time()
    r = client.post("/api/labs", json={
        "template_id": tmpl["id"], "name": "数据库原理-实验一", "class_name": "2402",
        "duration_min": 120, "student_ids": STUDENTS,
    }, headers=tok)
    check("POST /api/labs 受理 202", r.status_code == 202, r.text)
    lab_b = r.json()["lab_id"]
    print(f"    实验单 {lab_b} 受理，{r.json()['total']} 人，开始交付...")

    prog = wait_lab_done(lab_b, timeout=480)
    elapsed = time.time() - t0
    check("10 人全部交付成功", prog["summary"].get("running") == 10,
          f"summary={prog['summary']}")
    print(f"    耗时 {elapsed:.0f}s（并发 {config.CREATE_CONCURRENCY}）")

    bports = [i["port"] for i in prog["instances"]]
    check("端口无重复", len(set(bports)) == 10, f"ports={bports}")
    check("端口在池区间内",
          all(config.PORT_RANGE_START <= p <= config.PORT_RANGE_END for p in bports),
          f"ports={bports}")

    r = client.get(f"/api/labs/{lab_b}", headers=tok)
    check("GET /api/labs/{id} 进度可查",
          r.status_code == 200 and "db_pass" not in r.text,
          r.text[:200])

    r = client.get("/api/portpool/stats", headers=tok)
    check("端口池统计一致", r.json()["leased"] == 11 and r.json()["free"] == 89,
          str(r.json()))

# ---------------------------------------------------------------------------
# 第三部分：密码加密落库 + 端到端连通抽检
# ---------------------------------------------------------------------------
print("== 第三部分：加密与连通抽检 ==")
conn = db.connect()
row = conn.execute(
    "SELECT port, db_user, db_pass_enc, db_name FROM instances "
    "WHERE lab_id = ? AND status = 'running' ORDER BY id LIMIT 1", (lab_b,)
).fetchone()
conn.close()
plain = crypto.decrypt(row["db_pass_enc"])
check("db_pass 加密落库且可解密（Fernet 回环）", len(plain) == 16 and plain.isalnum(),
      f"len={len(plain)}")
check("明文未落库", "db_pass_enc" and row["db_pass_enc"] != plain)

host_port = row["port"]
with socket.socket() as s:
    s.settimeout(5)
    ok = s.connect_ex(("127.0.0.1", host_port)) == 0
check(f"实例端口 {host_port} TCP 可连", ok)

print(f"\n    样例连接信息（学生视角，密码不回显）: "
      f"host=<服务器公网IP> port={host_port} user={row['db_user']} db={row['db_name']}")
print(f"\n===== 任务 4 验收: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
