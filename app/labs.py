"""labs.py — 实验编排层（交接文档第八节任务 4）。

职责：把 ports（端口）+ docker_svc（容器）+ models（元数据）+ crypto（密码加密）
串成"给一个班批量交付独立 MySQL"的完整流程。

单个学生的创建流程（每步失败都有明确回滚，绝不留半成品）：
  1. lease_port                  —— 端口池租约（并发安全）
  2. create_mysql_instance       —— Docker 建容器（端口冲突换端口重试，坑清单 #2）
  3. create_instance             —— 元数据库登记（此时容器已建出，db_pass 加密落库）
  4. wait_until_running + ping   —— 健康确认（学生账号真实查询）
  5. status=running
失败路径：归还端口 + 兜底删容器 + instances.status=failed + last_error 留痕

班级批量：ThreadPoolExecutor 限 CREATE_CONCURRENCY（默认 5）并发，后台线程执行；
POST /api/labs 立即返回 lab_id，GET /api/labs/{id} 轮询进度（前端语义）。
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from . import config, crypto, db, docker_svc, insights, models, ports
from .errors import NotFound, PlatformError, PortPoolExhausted

log = logging.getLogger("mysql-lab.labs")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def create_lab_batch(teacher_id: int, template_id: int, name: str, class_name: str,
                     duration_min: int, student_ids: list[str]) -> int:
    """创建实验单并后台批量交付。返回 lab_id（立即返回，不等创建完成）。

    快速失败（不占后台线程）：空名单 / 名单超端口池容量。
    """
    # 去重、清洗；学号只允许字母数字_-（防容器名注入）
    cleaned = sorted({s.strip() for s in student_ids if s and s.strip()})
    if not cleaned:
        raise PlatformError("学生名单为空")
    bad = [s for s in cleaned if not re.fullmatch(r"[0-9A-Za-z_-]{1,32}", s)]
    if bad:
        raise PlatformError(f"学号格式非法（只允许字母/数字/_/-，≤32位）: {bad[:5]}")
    pool_total = config.PORT_RANGE_END - config.PORT_RANGE_START + 1
    if len(cleaned) > pool_total:
        raise PlatformError(
            f"名单 {len(cleaned)} 人超过端口池容量 {pool_total}，请分批创建"
        )
    tmpl = models.get_template(template_id)  # 不存在抛 NotFound → 404
    free = ports.stats()["free"]
    if len(cleaned) > free:
        raise PortPoolExhausted(
            f"空闲端口 {free} 个 < 名单 {len(cleaned)} 人，"
            f"请先回收实验或缩减规模"
        )
    if not (1 <= duration_min <= 24 * 60):
        raise PlatformError("duration_min 需在 1-1440 分钟之间")

    lab_id = models.create_lab(template_id, teacher_id, name, class_name,
                               duration_min)
    threading.Thread(
        target=_run_batch, args=(lab_id, tmpl, cleaned), daemon=True,
        name=f"lab-batch-{lab_id}",
    ).start()
    log.info("实验单 %s 已受理：%s 人，模板 %s", lab_id, len(cleaned), tmpl["name"])
    return lab_id


def lab_progress(lab_id: int) -> dict:
    """实验单进度（GET /api/labs/{id}）。不泄露 db_pass_enc。"""
    lab = models.get_lab(lab_id)
    instances = models.list_instances_by_lab(lab_id)
    summary: dict[str, int] = {}
    for i in instances:
        summary[i["status"]] = summary.get(i["status"], 0) + 1
    return {
        "lab_id": lab["id"],
        "name": lab["name"],
        "class_name": lab["class_name"],
        "status": lab["status"],
        "starts_at": lab["starts_at"],
        "ends_at": lab["ends_at"],
        "summary": summary,
        "instances": instances,
    }


def list_labs() -> list[dict]:
    """实验单列表（GET /api/labs）：含模板名与学生数，供教师端总览。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT l.id, l.name, l.class_name, l.status, l.duration_min, "
            "l.starts_at, l.ends_at, l.created_at, t.name AS template_name, "
            "(SELECT COUNT(*) FROM instances i WHERE i.lab_id = l.id) AS students "
            "FROM labs l JOIN templates t ON t.id = l.template_id "
            "ORDER BY l.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def export_credentials(lab_id: int) -> list[dict]:
    """导出某实验单全部学生的连接信息（含解密后的密码，仅教师角色路由调用）。

    这是教师分发票据的唯一出口：创建时明文只展示一次，之后从这里重新导出。
    """
    models.get_lab(lab_id)  # 不存在 → NotFound
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT student_id, port, db_name, db_user, db_pass_enc, status "
            "FROM instances WHERE lab_id = ? ORDER BY id", (lab_id,)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["db_password"] = crypto.decrypt(d.pop("db_pass_enc")) if d["db_pass_enc"] else ""
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 内部：批量执行
# ---------------------------------------------------------------------------
def _run_batch(lab_id: int, tmpl: dict, student_ids: list[str]) -> None:
    """后台线程：限流并发创建整单，结束后落实验单终态。"""
    models.update_lab_status(lab_id, "creating")
    results: list[dict] = []
    with ThreadPoolExecutor(
        max_workers=config.CREATE_CONCURRENCY, thread_name_prefix=f"lab{lab_id}"
    ) as pool:
        futures = {
            pool.submit(_create_one, lab_id, tmpl, sid): sid for sid in student_ids
        }
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # 编排层兜底：单人不该炸整单
                log.exception("学生 %s 创建线程异常", sid)
                results.append({"student_id": sid, "status": "failed",
                                "error": str(e)[:200]})

    n_running = sum(1 for r in results if r["status"] == "running")
    n_failed = sum(1 for r in results if r["status"] == "failed")
    lab_status = "running" if n_running > 0 else "failed"
    now = datetime.now().astimezone()
    ends = now + timedelta(minutes=models.get_lab(lab_id)["duration_min"])
    models.update_lab_status(lab_id, lab_status,
                             starts_at=now.isoformat(), ends_at=ends.isoformat())
    log.info("实验单 %s 完成：成功 %d，失败 %d", lab_id, n_running, n_failed)


def _create_one(lab_id: int, tmpl: dict, student_id: str) -> dict:
    """单个学生的完整交付，任何失败都回滚干净（端口/容器/错误留痕）。"""
    container = f"lab-{lab_id}-{student_id}"
    volume = str(config.LABS_DATA_DIR / str(lab_id) / student_id)
    safe = (re.sub(r"[^0-9a-zA-Z_]", "_", student_id)[:16]).strip("_") or "stu"
    db_name = f"lab_{safe}"
    db_user = f"stu_{safe}"[:32]  # MySQL 用户名上限 32 字符

    port: int | None = None
    creds: dict | None = None
    inst_id: int | None = None
    ever_leased: list[int] = []  # 本函数租过的端口（含已归还的），失败留痕用
    try:
        # 1-2) 租端口 + 建容器；端口被占（docker-proxy 释放竞态）换端口重试
        for attempt in range(1, config.CREATE_RETRIES + 1):
            port = ports.lease_port(container)
            ever_leased.append(port)
            creds = docker_svc.generate_credentials()
            try:
                docker_svc.create_mysql_instance(
                    lab_id, student_id, port, volume, tmpl["init_sql_path"],
                    db_name, db_user, creds["db_pass"], creds["root_password"],
                )
                break
            except docker_svc.ProviderError as e:
                ports.release_port(port)  # 换端口前必须先还旧租约
                port = None
                if attempt < config.CREATE_RETRIES and _is_port_conflict(str(e)):
                    log.warning("实验 %s 学生 %s 端口被占，第 %d 次换端口重试",
                                lab_id, student_id, attempt)
                    continue
                raise
        # 3) 登记（容器已建出；db_pass 用 Fernet 加密落库，不存明文）
        inst_id = models.create_instance(
            lab_id, student_id, container, volume, port, db_name, db_user,
            crypto.encrypt(creds["db_pass"]),
        )
        # 4) 健康确认：容器 running + 学生账号真实查询通
        #    （并发初始化下 mysqld 就绪慢，重试窗口见 config 注释坑 #9）
        docker_svc.wait_until_running(container, timeout=config.CREATE_WAIT_TIMEOUT)
        docker_svc.ping_mysql(container, db_user, creds["db_pass"], db_name,
                              retries=config.CREATE_PING_RETRIES,
                              interval=config.CREATE_PING_INTERVAL)
        # 4.5) 采集基线：把读取位置推到此刻，跳掉 entrypoint 灌入的初始化语句
        #      （不设的话，教师会把镜像初始化脚本当成"学生练了上万条"）
        try:
            insights.set_baseline(container, volume)
        except Exception as e:  # 基线失败不影响交付
            log.warning("实例 %s 设置采集基线失败: %s", container, e)
        models.update_instance_status(inst_id, "running")
        log.info("实验 %s 学生 %s 就绪（端口 %d）", lab_id, student_id, port)
        return {"student_id": student_id, "status": "running", "port": port}

    except Exception as e:
        # 回滚：端口必还、容器必清、错误必留痕
        if port is not None:
            ports.release_port(port)
        try:
            docker_svc.remove_instance(container)
        except Exception:
            pass
        err = f"{type(e).__name__}: {e}"[:500]
        if ever_leased:
            # 只要租过端口就留一条 failed 记录，教师端能看到失败原因
            # （端口已归还，failed 状态不占部分唯一索引，不影响复用）
            if inst_id is None:
                inst_id = models.create_instance(
                    lab_id, student_id, container, volume, ever_leased[-1],
                    db_name, db_user,
                    crypto.encrypt(creds["db_pass"]) if creds else "",
                )
            models.update_instance_status(inst_id, "failed", last_error=err)
        log.error("实验 %s 学生 %s 创建失败: %s", lab_id, student_id, err)
        return {"student_id": student_id, "status": "failed",
                "error": str(e)[:200]}


def _is_port_conflict(msg: str) -> bool:
    """Docker 端口占用类错误识别（坑清单 #2：docker-proxy 释放延迟）。"""
    m = msg.lower()
    return ("port is already allocated" in m
            or "address already in use" in m
            or "bind for" in m and "failed" in m)
