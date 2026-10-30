"""audit.py — 审计日志（交接文档第四节 audit_logs 表）。

登录、创建、重置、回收全留痕；任务 6 出审计页面，P1 出告警。
"""

from __future__ import annotations

from datetime import datetime

from . import db

_SCHEMA = {
    "audit_logs": """CREATE TABLE IF NOT EXISTS audit_logs (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        actor_id   INTEGER,
                        action     TEXT NOT NULL,
                        target     TEXT NOT NULL DEFAULT '',
                        detail     TEXT NOT NULL DEFAULT '',
                        ip         TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    )""",
}


def init() -> None:
    db.ensure_schema(_SCHEMA)


def log(actor_id: int | None, action: str, target: str = "",
        detail: str = "", ip: str = "") -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO audit_logs (actor_id, action, target, detail, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor_id, action, target, detail, ip,
             datetime.now().astimezone().isoformat()),
        )


def recent(limit: int = 100) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT a.id, a.actor_id, u.login AS actor, a.action, a.target, "
            "a.detail, a.ip, a.created_at "
            "FROM audit_logs a LEFT JOIN users u ON u.id = a.actor_id "
            "ORDER BY a.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
