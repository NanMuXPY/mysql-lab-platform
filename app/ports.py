"""ports.py — 端口池：租约、归还、对账。

对应交接文档第四节 port_pool 表 + 第八节任务 2 验收。
存储在统一元数据库 data/app.db（与任务 3 的 6 张表同库）。

并发安全（任务 2 双机压测验证）：
- 进程内 db._write_lock 串行化写事务 + SQLite BEGIN IMMEDIATE 防跨进程双租
- 分配策略：段内最小空闲端口，归还后可复用
- reconcile() 删除"租约有但实际没在用"的端口，防泄漏（最后一道防线）
"""

from __future__ import annotations

from datetime import datetime

from . import config, db
from .errors import PortPoolExhausted  # noqa: F401  (re-export)

_SCHEMA = {
    "port_pool": """CREATE TABLE IF NOT EXISTS port_pool (
                        port        INTEGER PRIMARY KEY,
                        instance_id TEXT NOT NULL,
                        leased_at   TEXT NOT NULL
                    )""",
}


def init() -> None:
    """幂等建表。入口（main.py）与测试脚本启动时调用。"""
    db.ensure_schema(_SCHEMA)


# ---------------------------------------------------------------------------
# 核心操作
# ---------------------------------------------------------------------------
def lease_port(instance_id: str) -> int:
    """为实例租一个端口（段内最小空闲），返回端口号。

    并发语义：N 个线程/进程同时调用各拿不同端口；
    耗尽抛 PortPoolExhausted，由调用方决定重试或缩减规模。
    """
    with db.write() as conn:
        leased = {r[0] for r in conn.execute("SELECT port FROM port_pool").fetchall()}
        for port in range(config.PORT_RANGE_START, config.PORT_RANGE_END + 1):
            if port not in leased:
                conn.execute(
                    "INSERT INTO port_pool (port, instance_id, leased_at) VALUES (?, ?, ?)",
                    (port, instance_id, datetime.now().astimezone().isoformat()),
                )
                return port
        raise PortPoolExhausted(
            f"端口段 {config.PORT_RANGE_START}-{config.PORT_RANGE_END} 已全部租出"
            f"（{config.PORT_RANGE_END - config.PORT_RANGE_START + 1} 个）"
        )


def release_port(port: int) -> bool:
    """归还单个端口。返回是否确有租约被删（幂等：重复归还返回 False）。"""
    with db.write() as conn:
        cur = conn.execute("DELETE FROM port_pool WHERE port = ?", (port,))
        return cur.rowcount > 0


def release_by_instance(instance_id: str) -> list[int]:
    """归还某实例租用的全部端口（回收整单用），返回归还的端口列表。"""
    with db.write() as conn:
        rows = conn.execute(
            "SELECT port FROM port_pool WHERE instance_id = ?", (instance_id,)
        ).fetchall()
        ports = [r[0] for r in rows]
        conn.execute("DELETE FROM port_pool WHERE instance_id = ?", (instance_id,))
        return ports


def reconcile(actual_ports: set[int]) -> list[int]:
    """对账：强制归还未在 actual_ports 中的租约端口（防泄漏，巡检任务用）。

    actual_ports: 当前真实监听的宿主端口集合（从 Docker 采集）。
    返回被释放的端口列表。
    """
    with db.write() as conn:
        rows = conn.execute("SELECT port FROM port_pool").fetchall()
        stale = [r[0] for r in rows if r[0] not in actual_ports]
        for port in stale:
            conn.execute("DELETE FROM port_pool WHERE port = ?", (port,))
        return stale


def leased_ports() -> dict[int, str]:
    """当前全部租约 {port: instance_id}。"""
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT port, instance_id FROM port_pool").fetchall())
    finally:
        conn.close()


def stats() -> dict:
    """池状态总览（状态看板用）。"""
    total = config.PORT_RANGE_END - config.PORT_RANGE_START + 1
    leased = len(leased_ports())
    return {
        "range": f"{config.PORT_RANGE_START}-{config.PORT_RANGE_END}",
        "total": total,
        "leased": leased,
        "free": total - leased,
    }
