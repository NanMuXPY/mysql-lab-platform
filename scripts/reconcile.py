"""对账（reconcile）：让元数据库状态与实际 Docker、端口池一致。

为什么必须有这个脚本：
- 容器可能被人工 `docker rm`、被 OOM 杀、被 `docker system prune` 清掉，
  甚至在 `docker compose down` 时连带删除；而元数据库里仍记着 running，
  教师端会显示"学生环境正常" —— 学生却连不上。这类"僵尸记录"是
  交接文档点名最容易漏的坑。
- 实验单（labs）层级状态是从各个实例状态汇总来的，实例变了它不一定跟着变，
  也需要一起重算，否则列表页永远显示"running"。

做法（幂等，可反复执行）：
  0. 到期实验单（ends_at 已过且 status=running）→ 自动回收：
     删容器 + 删数据卷 + 还端口，实例与实验单置 recycled（任务 5）
  1. 实例状态 in (creating/running/stopped) 但 Docker 里找不到容器
       → 置 recycled，归还端口租约
  2. 按实例真实状态重算实验单状态
  3. 端口池里指向不存在实例的租约 → 归还

用法：python3.11 scripts/reconcile.py [--dry-run]
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, docker_svc, ports  # noqa: E402

ACTIVE = ("creating", "running", "stopped")


def main(dry_run: bool = False) -> int:
    try:
        live = {c.name for c in docker_svc._client().containers.list(all=True)}
    except Exception as e:
        print(f"连不上 Docker，无法对账：{e}")
        return 2

    # 0) 到期实验单自动回收（任务 5：时长用完即释放，防 8G 内存被吃满）
    expired_n = _recycle_expired(dry_run)

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, lab_id, student_id, container_name, port, status "
            "FROM instances WHERE status IN (?,?,?)", ACTIVE
        ).fetchall()
    finally:
        conn.close()

    ghosts = [r for r in rows if r["container_name"] not in live]
    print(f"活跃记录 {len(rows)} 条，其中容器已消失 {len(ghosts)} 条")
    for r in ghosts:
        print(f"  - lab{r['lab_id']} {r['student_id']} {r['container_name']}"
              f" :{r['port']} [{r['status']}]")

    if ghosts and not dry_run:
        _fix_instances(ghosts)

    # 1.5) 反向僵尸：容器还活着但元数据库已无活跃记录（回收漏删、半成品等）→ 删容器
    active_names = {r["container_name"] for r in rows}
    orphans = [n for n in live
               if n.startswith("lab-") and n not in active_names]
    if orphans:
        print(f"反向僵尸容器（无活跃记录但仍在运行）{len(orphans)} 个")
        for n in orphans:
            print(f"  - {n}")
        if not dry_run:
            for n in orphans:
                try:
                    docker_svc.remove_instance(n)
                    print(f"    已删除 {n}")
                except Exception as e:
                    print(f"    删除失败（下次对账重试）：{e}")

    # 2) 实验单状态重算（dry-run 只打印建议，不写库）
    fixed_labs = 0
    with db.write() as c:
        labs = c.execute("SELECT id, name, status FROM labs").fetchall()
        for lab in labs:
            stats: dict[str, int] = {}
            for row in c.execute(
                "SELECT status, COUNT(*) n FROM instances WHERE lab_id=? GROUP BY status",
                (lab["id"],),
            ):
                stats[row["status"]] = row["n"]
            if not stats:
                new = lab["status"]
            elif stats.get("running") or stats.get("creating"):
                new = "running"
            elif stats.get("stopped"):
                new = "stopped"
            elif stats.get("failed") and not stats.get("recycled"):
                new = "failed"
            else:
                new = "recycled"
            if new != lab["status"]:
                print(f"  实验单 #{lab['id']} {lab['name']} 状态 {lab['status']} → {new}")
                if not dry_run:
                    c.execute("UPDATE labs SET status=? WHERE id=?", (new, lab["id"]))
                fixed_labs += 1

    if dry_run:
        print(f"\n[dry-run] 到期回收 {expired_n} 个、待纠正实例 {len(ghosts)} 条、"
              f"实验单 {fixed_labs} 条；未写库。")
        return 0
    print(f"\n完成：到期回收 {expired_n} 个，纠正实例 {len(ghosts)} 条，实验单 {fixed_labs} 条")
    print("端口池：", ports.stats())
    return 0


def _recycle_expired(dry_run: bool) -> int:
    """ends_at 已过且仍在 running 的实验单 → 整单自动回收（幂等）。"""
    now = datetime.now().astimezone()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, ends_at FROM labs WHERE status = 'running'"
        ).fetchall()
    finally:
        conn.close()

    expired = []
    for lab in rows:
        try:
            ends = datetime.fromisoformat(lab["ends_at"])
        except (TypeError, ValueError):
            continue  # 无 ends_at（pending/creating 中）或格式异常，跳过
        if ends.tzinfo is None:
            ends = ends.astimezone()
        if ends < now:
            expired.append(dict(lab))

    for lab in expired:
        print(f"到期回收：实验单 #{lab['id']} {lab['name']}（{lab['ends_at']} 已过期）")
        if dry_run:
            continue
        try:
            from app import labs as labs_mod  # 延迟导入避免循环依赖
            r = labs_mod.recycle_lab(lab["id"])
            print(f"    已回收 {r['recycled']} 个实例")
        except Exception as e:
            print(f"    回收失败（下次对账重试）：{e}")
    return len(expired)


def _fix_instances(ghosts: list) -> None:
    """僵尸实例：置 recycled + 归还端口 + 兜底删容器（幂等）。"""
    for r in ghosts:
        if r["port"]:
            try:
                ports.release_port(r["port"])
            except Exception as e:
                print(f"    端口 {r['port']} 归还失败（可忽略）：{e}")
        try:
            docker_svc.remove_instance(r["container_name"])  # 幂等兜底
        except Exception:
            pass
        with db.write() as c:
            c.execute(
                "UPDATE instances SET status='recycled', last_error=? WHERE id=?",
                ("对账：容器已不存在，状态自动纠正为 recycled", r["id"]),
            )


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
