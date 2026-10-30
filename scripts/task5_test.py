"""task5_test.py — 任务 5 验收：实例生命周期（服务器上运行）。

验收点：
1. stop_lab：running → stopped，容器退出、数据卷保留、端口租约保留
2. reset_instance：stopped/running → 重建容器，同端口，密码更换，学生账号可用
3. recycle_lab：容器删除 + 数据卷删除 + 端口归还，状态机全量落 recycled
4. 非法状态守卫：重复回收 / 对 recycled 重置 / 对非 running 停止 → 业务报错
5. 导出 CSV 反映重置后的新密码

运行: cd /root/mysql-lab && python3.11 scripts/task5_test.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, docker_svc, labs, models, ports  # noqa: E402
from app.errors import PlatformError  # noqa: E402

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


def container_state(name: str) -> str | None:
    info = docker_svc.get_instance_status(name)
    return info["status"] if info else None


def main() -> None:
    models.init()
    ports.init()
    tmpl = models.list_templates()[0]
    print(f"== 任务 5 验收开始（模板: {tmpl['name']}）==\n")

    free_before = ports.stats()["free"]

    # ---- 准备：建 1 人实验单并等交付完成 ----
    print("-- 准备：创建测试实验单（1 人）")
    lab_id = labs.create_lab_batch(
        teacher_id=1, template_id=tmpl["id"], name="任务5验收-生命周期",
        class_name="验收班", duration_min=120, student_ids=["950001"],
    )
    p = wait_lab_done(lab_id)
    check("1.1 实验单交付完成且 running", p["status"] == "running", str(p["summary"]))
    inst = models.list_instances_by_lab(lab_id)[0]
    inst_id = inst["id"]
    old_pwd = labs.export_credentials(lab_id)[0]["db_password"]
    check("1.2 实例 running 且容器存活",
          inst["status"] == "running" and container_state(inst["container_name"]) == "running")

    # ---- 2. 停止 ----
    print("\n-- 停止：stop_lab")
    r = labs.stop_lab(lab_id)
    check("2.1 返回 stopped=1", r["stopped"] == 1, str(r))
    check("2.2 实例状态 stopped",
          models.list_instances_by_lab(lab_id)[0]["status"] == "stopped")
    check("2.3 容器已退出（数据卷保留）", container_state(inst["container_name"]) == "stopped")
    check("2.4 端口租约保留（stopped 占端口）",
          inst["port"] in ports.leased_ports())
    check("2.5 实验单状态 stopped", models.get_lab(lab_id)["status"] == "stopped")

    # ---- 3. 重置 ----
    print("\n-- 重置：reset_instance（同端口重建，密码更换，约 1 分钟）")
    r = labs.reset_instance(inst_id)
    check("3.1 端口不变", r["port"] == inst["port"], str(r["port"]))
    check("3.2 密码已更换", r["db_password"] != old_pwd)
    new_pwd = labs.export_credentials(lab_id)[0]["db_password"]
    check("3.3 CSV 导出反映新密码", new_pwd == r["db_password"])
    check("3.4 实例回到 running",
          models.list_instances_by_lab(lab_id)[0]["status"] == "running")
    check("3.5 学生账号真实可查（ping 通过）",
          docker_svc.ping_mysql(inst["container_name"], inst["db_user"],
                                r["db_password"], inst["db_name"], retries=5))
    check("3.6 旧密码已失效",
          _ping_expect_fail(inst["container_name"], inst["db_user"],
                            old_pwd, inst["db_name"]))

    # ---- 4. 回收 ----
    print("\n-- 回收：recycle_lab（不可逆）")
    r = labs.recycle_lab(lab_id)
    check("4.1 返回 recycled=1", r["recycled"] == 1, str(r))
    check("4.2 容器已删除", container_state(inst["container_name"]) is None)
    check("4.3 数据卷已删除", not (config.LABS_DATA_DIR / str(lab_id) / "950001").exists())
    check("4.4 端口已归还", inst["port"] not in ports.leased_ports())
    check("4.5 实例与实验单均 recycled",
          models.list_instances_by_lab(lab_id)[0]["status"] == "recycled"
          and models.get_lab(lab_id)["status"] == "recycled")
    check("4.6 端口池恢复（free 与开始一致）", ports.stats()["free"] == free_before)

    # ---- 5. 非法状态守卫 ----
    print("\n-- 非法状态守卫")
    try:
        labs.recycle_lab(lab_id)
        check("5.1 重复回收被拒绝", False)
    except PlatformError:
        check("5.1 重复回收被拒绝", True)
    try:
        labs.reset_instance(inst_id)
        check("5.2 对 recycled 实例重置被拒绝", False)
    except PlatformError:
        check("5.2 对 recycled 实例重置被拒绝", True)
    try:
        labs.stop_lab(lab_id)
        check("5.3 对非 running 实验单停止被拒绝", False)
    except PlatformError:
        check("5.3 对非 running 实验单停止被拒绝", True)

    # ---- 汇总 ----
    print(f"\n== 结果: {PASS} PASS / {FAIL} FAIL ==")
    sys.exit(1 if FAIL else 0)


def _ping_expect_fail(container: str, user: str, pwd: str, dbn: str) -> bool:
    """旧密码应当连不上（返回 False 才算通过）。"""
    try:
        docker_svc.ping_mysql(container, user, pwd, dbn, retries=2, interval=1)
        return False
    except Exception:
        return True


if __name__ == "__main__":
    main()
