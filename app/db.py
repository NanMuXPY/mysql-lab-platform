"""db.py — SQLite 公共数据访问层（ports.py 与任务 3 的 models.py 共用）。

并发模型（已在任务 2 压测验证）：
- 所有模块共享一把进程内写锁 _write_lock，串行化本进程写事务
- 连接层 WAL + busy_timeout=10s，防御跨进程写冲突（如手动 sqlite3 CLI 对账）
- tx() 用 BEGIN IMMEDIATE：先拿写锁再读，消除 SELECT→INSERT 竞态窗口

约定：
- 每次操作短连接（connect→操作→close），不做长连接池（SQLite 语境下反而稳）
- 表结构初始化统一走 ensure_schema()，各模块把建表语句注册进来（任务 3 合并 6 张表）
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from . import config

# 进程内写锁：线程池批量创建（任务 4）时保护写事务
_write_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    """新建短连接，已设 WAL / busy_timeout / 外键约束。"""
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务上下文。异常自动回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def write() -> "_WriteTx":
    """带进程锁的写事务：with db.write() as conn: ...（锁自动持有/释放）。"""
    return _WriteTx()


class _WriteTx:
    def __enter__(self) -> sqlite3.Connection:
        _write_lock.acquire()
        self._conn = connect()
        self._ctx = tx(self._conn)
        return self._ctx.__enter__()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._ctx.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()
            _write_lock.release()


def ensure_schema(statements: dict[str, str]) -> None:
    """幂等建表。statements: {表名: CREATE TABLE IF NOT EXISTS ...}"""
    with _write_lock:
        conn = connect()
        try:
            with tx(conn):
                for ddl in statements.values():
                    conn.execute(ddl)
        finally:
            conn.close()
