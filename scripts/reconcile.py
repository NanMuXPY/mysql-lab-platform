"""对账（reconcile）：让元数据库状态与实际 Docker、端口池一致。

为什么必须有这个脚本：
- 容器可能被人工 `docker rm`、被 OOM 杀、被 `docker system prune` 清掉，
  甚至在 `docker compose down` 时连带删除；而元数据库里仍记着 running，
  教师端会显示"学生环境正常" —— 学生却连不上。这类"僵尸记录"是
  交接文档点名最容易漏的坑。
- 实验单（labs）层级状态是从各个实例状态汇总来的，实例变了它不一定跟着变，
  也需要一起重算，否则列表页永远显示"running"。

做法（幂等，可反复执行）：
  1. 实例状态 in (creating/running/stopped) 但 Docker 里找不到容器
       → 置 recycled，归还端口租约
  2. 按实例真实状态重算实验单状态
  3. 端口池里指向不存在实例的租约 → 归还

用法：python3.11 scripts/reconcile.py [--dry-run]
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, docker_svc, ports  # noqa: E402

ACTIVE = ("creating", "running", "stopped")


def main(dry_run: bool = False) -> int:
    try:
        live = {c.name for c in docker_svc._client().containers.list(all=True)}
    except Exception as e:
        print(f"连不上 Docker，无法对账：{e}")
        return 2

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
        print(f"\n[dry-run] 待纠正实例 {len(ghosts)} 条、实验单 {fixed_labs} 条；未写库。")
        return 0
    print(f"\n完成：纠正实例 {len(ghosts)} 条，实验单 {fixed_labs} 条")
    print("端口池：", ports.stats())
    return 0


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
