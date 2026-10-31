"""models.py — 元数据库 6 张表（交接文档第四节）。

users / templates / labs / instances / port_pool(在 ports.py) / audit_logs(在 audit.py)
全部注册进 db.ensure_schema() 统一建表，同库 data/app.db。

本模块只放表结构与最小 CRUD；批量编排逻辑在 labs.py（任务 4）。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime

from . import db
from .errors import NotFound

_SCHEMA = {
    "users": """CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    login         TEXT NOT NULL UNIQUE,
                    name          TEXT NOT NULL,
                    role          TEXT NOT NULL CHECK (role IN ('teacher','assistant','student')),
                    password_hash TEXT NOT NULL,
                    created_at    TEXT NOT NULL
                )""",
    "templates": """CREATE TABLE IF NOT EXISTS templates (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    name         TEXT NOT NULL,
                    description  TEXT NOT NULL DEFAULT '',
                    init_sql_path TEXT NOT NULL,
                    version      TEXT NOT NULL DEFAULT 'v1',
                    created_at   TEXT NOT NULL
                )""",
    "labs": """CREATE TABLE IF NOT EXISTS labs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id  INTEGER NOT NULL REFERENCES templates(id),
                    teacher_id   INTEGER NOT NULL REFERENCES users(id),
                    name         TEXT NOT NULL,
                    class_name   TEXT NOT NULL DEFAULT '',
                    duration_min INTEGER NOT NULL,
                    allowed_cidr TEXT NOT NULL DEFAULT '',
                    status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','creating','running','stopped','recycled','failed')),
                    starts_at    TEXT,
                    ends_at      TEXT,
                    created_at   TEXT NOT NULL
                )""",
    "instances": """CREATE TABLE IF NOT EXISTS instances (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    lab_id         INTEGER NOT NULL REFERENCES labs(id),
                    student_id     TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    volume_path    TEXT NOT NULL,
                    port           INTEGER NOT NULL,
                    db_name        TEXT NOT NULL,
                    db_user        TEXT NOT NULL,
                    db_pass_enc    TEXT NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'creating'
                                   CHECK (status IN ('creating','running','stopped','recycled','failed')),
                    created_at     TEXT NOT NULL,
                    last_error     TEXT
                )""",
    # 端口唯一性只约束"活跃"实例：recycled/failed 的历史记录不能挡住端口复用
    # （回收再分配是本平台核心语义，全表 UNIQUE 会让端口越用越少——坑清单 #8）
    "idx_instances_active_port": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_active_port "
        "ON instances(port) WHERE status IN ('creating','running','stopped')"
    ),
}


def init() -> None:
    db.ensure_schema(_SCHEMA)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


# ---------------------------------------------------------------------------
# 密码哈希：PBKDF2-HMAC-SHA256（纯标准库，刻意不用 passlib——
# 它与 bcrypt>=4.1 有已知的 __about__ 兼容性坑，教学平台不值得引入）
# 格式: pbkdf2$<iterations>$<salt_hex>$<hash_hex>
# ---------------------------------------------------------------------------
_PBKDF2_ITERS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
def create_user(login: str, name: str, role: str, password: str) -> int:
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO users (login, name, role, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (login, name, role, hash_password(password), _now()),
        )
        return cur.lastrowid


def get_user_by_login(login: str) -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_users() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------
def create_template(name: str, description: str, init_sql_path: str, version: str = "v1") -> int:
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO templates (name, description, init_sql_path, version, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, description, init_sql_path, version, _now()),
        )
        return cur.lastrowid


def list_templates() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, description, version FROM templates ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_template(template_id: int) -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"模板 {template_id} 不存在")
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# labs / instances（任务 4 的编排层调用；先备好最小读写）
# ---------------------------------------------------------------------------
def create_lab(template_id: int, teacher_id: int, name: str, class_name: str,
               duration_min: int, allowed_cidr: str = "") -> int:
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO labs (template_id, teacher_id, name, class_name, duration_min, "
            "allowed_cidr, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (template_id, teacher_id, name, class_name, duration_min, allowed_cidr, _now()),
        )
        return cur.lastrowid


def update_lab_status(lab_id: int, status: str, starts_at: str | None = None,
                      ends_at: str | None = None) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE labs SET status = ?, starts_at = COALESCE(?, starts_at), "
            "ends_at = COALESCE(?, ends_at) WHERE id = ?",
            (status, starts_at, ends_at, lab_id),
        )


def get_lab(lab_id: int) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
        if not row:
            raise NotFound(f"实验单 {lab_id} 不存在")
        return dict(row)
    finally:
        conn.close()


def create_instance(lab_id: int, student_id: str, container_name: str,
                    volume_path: str, port: int, db_name: str, db_user: str,
                    db_pass_enc: str) -> int:
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO instances (lab_id, student_id, container_name, volume_path, port, "
            "db_name, db_user, db_pass_enc, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?)",
            (lab_id, student_id, container_name, volume_path, port,
             db_name, db_user, db_pass_enc, _now()),
        )
        return cur.lastrowid


def update_instance_status(instance_id: int, status: str,
                           last_error: str | None = None) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE instances SET status = ?, last_error = ? WHERE id = ?",
            (status, last_error, instance_id),
        )


def list_instances_by_lab(lab_id: int) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, student_id, container_name, volume_path, port, db_name, "
            "db_user, status, last_error FROM instances WHERE lab_id = ? ORDER BY id",
            (lab_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_active_instance_by_student(student_id: str) -> dict | None:
    """按学号找该学生**最新**的活跃实例（含实验单信息），学生端登录用。

    同一学号会横跨多个实验单（一学期十几次实验），取最新创建的那条：
    学生手里记着的密码一定是最近一次发给他的，老实验单的密码大概率已随重置作废。
    返回 dict 含 db_pass_enc（调用方自行解密比对，不要直接吐给前端）。
    """
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT i.*, l.name AS lab_name, l.class_name AS class_name, "
            "       l.status AS lab_status "
            "FROM instances i JOIN labs l ON l.id = i.lab_id "
            "WHERE i.student_id = ? AND i.status IN ('creating','running','stopped') "
            "ORDER BY i.id DESC LIMIT 1",
            (student_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_instance(instance_id: int) -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"实例 {instance_id} 不存在")
        return dict(row)
    finally:
        conn.close()


def update_instance_credentials(instance_id: int, db_pass_enc: str) -> None:
    """重置后更换学生密码（Fernet 密文）。"""
    with db.write() as conn:
        conn.execute(
            "UPDATE instances SET db_pass_enc = ? WHERE id = ?",
            (db_pass_enc, instance_id),
        )


# ---------------------------------------------------------------------------
# 演示数据种子（users 为空时调用；仅开发环境，正式部署应走管理命令建号）
# ---------------------------------------------------------------------------
def seed_demo_users() -> dict:
    """种子账号只在首次启动、users 表为空时创建，密码打印一次到日志。"""
    if count_users() > 0:
        return {}
    creds = {
        "teacher": ("teacher", "王老师", os.environ.get("TEACHER_INIT_PASSWORD", "<演示用默认密码，部署时须修改>")),
        "assistant": ("assistant", "李助教", os.environ.get("ASSISTANT_INIT_PASSWORD", "Assist@2026")),
        "student": ("240101", "张三", os.environ.get("STUDENT_INIT_PASSWORD", "Stu@2026")),
    }
    out = {}
    for role, (login, name, pwd) in creds.items():
        create_user(login, name, role, pwd)
        out[login] = pwd
    return out


def seed_demo_template() -> int | None:
    from pathlib import Path
    sql_path = Path(__file__).resolve().parent.parent / "templates_sql" / "sample_basic.sql"
    if not list_templates():
        return create_template(
            "基础三表实验", "student/course/score 三表 + 十条初始数据（样例模板）",
            str(sql_path),
        )
    return None


def seed_presets() -> list[str]:
    """注册 templates_sql/preset_*.sql 预设课程（按模板名幂等，重复启动不重）。

    新增预设课程 = 往 templates_sql/ 丢一个 preset_*.sql + 在这里登记一行。
    """
    from pathlib import Path
    sql_dir = Path(__file__).resolve().parent.parent / "templates_sql"
    presets = [
        ("preset_single_table.sql", "单表查询与聚合",
         "emp/dept 员工部门表：SELECT/WHERE/ORDER BY/聚合/GROUP BY/HAVING"),
        ("preset_library.sql", "图书借阅管理",
         "book/reader/borrow：外键约束、多表连接、日期计算、子查询"),
        ("preset_ecommerce.sql", "电商订单分析",
         "customer/product/orders：多表 JOIN、金额汇总、TOP-N 查询"),
        ("preset_joins.sql", "多表连接进阶",
         "院系/学生/选课：INNER/LEFT JOIN、EXISTS、复合主键"),
    ]
    existing = {t["name"] for t in list_templates()}
    added: list[str] = []
    for fname, name, desc in presets:
        p = sql_dir / fname
        if p.is_file() and name not in existing:
            create_template(name, desc, str(p))
            added.append(name)
    return added
