"""insights.py — 学生操作采集与错题诊断（P1：让教师看见"学生怎么错的"）。

思路（为什么这么做）：
- MySQL 的 general_log 会记录**客户端发来的每一条 SQL**，包括执行失败的语句，
  因此它天然就是"学生练习痕迹"的采集点，不需要改学生客户端、不需要代理层。
- 报错文本不在 general log 里（那是 error log 的事），所以错误判定放在平台侧：
  用 sqlglot（MySQL 方言解析器）解析学生 SQL，解析失败即语法错误；
  再用 AST 抓教学价值最高的两类坑：UPDATE/DELETE 无 WHERE（危险操作）、
  JOIN 缺 ON（笛卡尔积）、SELECT *（写法提示）。
- 采集是**增量**的：每个实例记录已读字节偏移，只解析新增部分，轮询成本极低。

存储：student_queries（问题语句留档）+ instance_log_offsets（读偏移）。
日志文件位置：数据卷内 general.log（bind mount，平台直接读文件，无需 docker exec）。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from . import db, models

log = logging.getLogger("mysql-lab.insights")

_SCHEMA = {
    "student_queries": """CREATE TABLE IF NOT EXISTS student_queries (
                            id          INTEGER PRIMARY KEY AUTOINCREMENT,
                            lab_id      INTEGER NOT NULL,
                            instance_id TEXT NOT NULL,
                            student_id  TEXT NOT NULL,
                            ts          TEXT NOT NULL,
                            sql_text    TEXT NOT NULL,
                            verdict     TEXT NOT NULL,
                            detail      TEXT NOT NULL DEFAULT '',
                            created_at  TEXT NOT NULL
                        )""",
    "idx_student_queries": (
        "CREATE INDEX IF NOT EXISTS idx_student_queries_lab "
        "ON student_queries(lab_id, id)"
    ),
    "instance_log_offsets": """CREATE TABLE IF NOT EXISTS instance_log_offsets (
                                  instance_id TEXT PRIMARY KEY,
                                  offset      INTEGER NOT NULL DEFAULT 0,
                                  updated_at  TEXT NOT NULL
                              )""",
}

LOG_NAME = "general.log"

# general log 文件行格式（MySQL 8）：
#   2026-10-30T09:00:32.586497Z\t   10 Query\tSELECT 1
# 注意：时间戳是 UTC，末尾带 Z（容器 TZ 不影响日志时间戳）
_LINE_RE = re.compile(
    r"^(?P<ts>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z?)\s+(?P<tid>\d+)\s+"
    r"(?P<cmd>\w+)(?:\s(?P<arg>.*))?$"
)

# 客户端/平台的机械语句，不具教学价值，直接过滤
_NOISE_PREFIX = (
    "SET ", "SHOW ", "USE ", "COMMIT", "ROLLBACK", "START TRANSACTION", "BEGIN",
    "FLUSH ", "SELECT 1", "SELECT @@", "SELECT DATABASE()", "SELECT NOW()",
    "SELECT CONNECTION_ID()", "SELECT USER()", "SELECT CURRENT_USER()",
    # mysql 官方镜像 entrypoint 用 PREPARE/EXECUTE 循环执行初始化 SQL，
    # 开启 general_log 后这些内部语句会灌进几万条，必须挡掉
    "PREPARE ", "EXECUTE ", "DEALLOCATE ", "DROP PREPARE",
)


def init() -> None:
    db.ensure_schema(_SCHEMA)


def set_baseline(container_name: str, volume_path: str) -> int:
    """把采集基线推到日志文件当前末尾。

    必须在容器交付完成（初始化数据就绪）后调用：mysql 镜像的 entrypoint 会用
    PREPARE/EXECUTE 循环执行初始化 SQL，开启 general_log 后这部分会被完整记录
    （实测 6.4MB / 1.6 万条）。若不设基线，教师端会把"初始化脚本"统计成
    "学生练了 1 万多条"，整个诊断失去意义。
    """
    path = os.path.join(volume_path, LOG_NAME)
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    now = datetime.now().astimezone().isoformat()
    with db.write() as c:
        c.execute(
            "INSERT INTO instance_log_offsets (instance_id, offset, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(instance_id) DO UPDATE SET "
            "offset = excluded.offset, updated_at = excluded.updated_at",
            (container_name, size, now),
        )
    log.info("采集基线已设置 %s -> %d 字节", container_name, size)
    return size


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def collect_lab(lab_id: int) -> dict:
    """增量采集一个实验单全部实例的新增 SQL，返回本轮统计。

    每个实例额外查一次"库里有哪些表"，用于识别"表名拼错"这类常见失误
    （语法合法但执行会失败 —— 1064 纯语法错 MySQL 不写 general log，抓不到）。
    """
    conn = db.connect()
    try:
        instances = conn.execute(
            "SELECT id, student_id, container_name, volume_path, db_name FROM instances "
            "WHERE lab_id = ? AND status IN ('running','stopped')", (lab_id,)
        ).fetchall()
        offsets = {r["instance_id"]: r["offset"] for r in conn.execute(
            "SELECT instance_id, offset FROM instance_log_offsets").fetchall()}
    finally:
        conn.close()

    new_rows: list[tuple] = []
    new_offsets: dict[str, int] = {}
    now = datetime.now().astimezone().isoformat()

    for inst in instances:
        path = os.path.join(inst["volume_path"], LOG_NAME)
        if not os.path.isfile(path):
            continue
        offset = offsets.get(inst["container_name"])
        if offset is None:
            # 没有基线 = 老实例或异常路径：从文件末尾开始，绝不从 0 全量灌一遍
            # （否则初始化脚本的几万条会一次性当成学生练习入库）
            try:
                offset = os.path.getsize(path)
            except OSError:
                offset = 0
            log.warning("实例 %s 无采集基线，从当前位置 %d 开始", inst["container_name"], offset)
        tables = _instance_tables(inst["container_name"], inst["db_name"])
        try:
            size = os.path.getsize(path)
            if size <= offset:
                if size < offset:  # 文件被重置：从零重读
                    new_offsets[inst["container_name"]] = 0
                continue
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
            # 只消费到最后一个换行，避免半行
            cut = chunk.rfind(b"\n")
            if cut < 0:
                continue
            body, consumed = chunk[:cut + 1], offset + cut + 1
            new_offsets[inst["container_name"]] = consumed
            for line in body.decode("utf-8", "replace").splitlines():
                row = _parse_line(line, lab_id, inst["container_name"],
                                  inst["student_id"], now, tables)
                if row:
                    new_rows.append(row)
        except OSError as e:
            log.warning("实例 %s 日志读取失败: %s", inst["container_name"], e)

    if new_rows or new_offsets:
        with db.write() as c:
            c.executemany(
                "INSERT INTO student_queries (lab_id, instance_id, student_id, ts, "
                "sql_text, verdict, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                new_rows,
            )
            for cid, off in new_offsets.items():
                c.execute(
                    "INSERT INTO instance_log_offsets (instance_id, offset, updated_at) "
                    "VALUES (?,?,?) ON CONFLICT(instance_id) DO UPDATE SET "
                    "offset = excluded.offset, updated_at = excluded.updated_at",
                    (cid, off, now),
                )
    return {"instances": len(instances), "new_queries": len(new_rows),
            "new_problems": sum(1 for r in new_rows if r[5] != "ok")}


def _instance_tables(container: str, db_name: str) -> set[str] | None:
    """取学生库当前表名集合（docker exec 查 information_schema）。

    失败返回 None（调用方跳过表名校验，不影响采集主流程）。
    """
    import subprocess
    try:
        pw = subprocess.run(
            ["docker", "exec", container, "printenv", "MYSQL_ROOT_PASSWORD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not pw:
            return None
        sql = (f"SELECT table_name FROM information_schema.tables "
               f"WHERE table_schema='{db_name}'")
        r = subprocess.run(
            ["docker", "exec", container, "mysql", "-h127.0.0.1", "-uroot",
             f"-p{pw}", "-N", "-B", "-e", sql],
            capture_output=True, text=True, timeout=15,
        )
        return {ln.strip().lower() for ln in r.stdout.splitlines() if ln.strip()}
    except Exception as e:  # 容器不在/超时等：降级为不校验
        log.debug("取表清单失败 %s: %s", container, e)
        return None


def _parse_line(line: str, lab_id: int, instance_id: str, student_id: str,
                now: str, tables: set[str] | None = None) -> tuple | None:
    m = _LINE_RE.match(line)
    if not m or m.group("cmd") != "Query":
        return None  # 只关心 Query（Connect/Quit 等忽略）
    sql = (m.group("arg") or "").strip()
    if not sql:
        return None
    verdict, detail = classify(sql, tables)
    if verdict == "noise":
        return None
    # 日志时间戳是 UTC（带 Z），转成本地时区再存
    ts_raw = m.group("ts")
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) \
            .astimezone().isoformat(timespec="seconds")
    except ValueError:
        ts = ts_raw
    return (lab_id, instance_id, student_id, ts, sql[:2000], verdict, detail, now)


# ---------------------------------------------------------------------------
# 诊断：sqlglot 解析 + 教学高频坑检测
# ---------------------------------------------------------------------------
def classify(sql: str, tables: set[str] | None = None) -> tuple[str, str]:
    """返回 (verdict, detail)。

    verdict: ok | syntax_error | unknown_table | dangerous | hint | noise
    tables: 学生库现有表名集合（小写）；提供时启用"表名拼错"校验。
    """
    s = sql.strip().rstrip(";").strip()
    up = s.upper()
    for p in _NOISE_PREFIX:
        if up.startswith(p.upper()):
            return "noise", ""
    try:
        trees = [t for t in sqlglot.parse(s, read="mysql") if t is not None]
    except ParseError as e:
        desc = "SQL 语法无法解析"
        if getattr(e, "errors", None):
            desc = e.errors[0].get("description") or desc
        return "syntax_error", str(desc)[:160]
    if not trees:
        return "noise", ""
    # sqlglot 对某些乱写会"宽容"解析成非语句节点（如 SELECT FROM emp → 空查询、
    # SELEC * FORM emp → 列别名），这类必须判错，否则会把学生的错当对
    allowed = (exp.Select, exp.Insert, exp.Update, exp.Delete, exp.Create,
               exp.Drop, exp.Alter, exp.Describe, exp.Show, exp.Command,
               exp.Use, exp.Commit, exp.Rollback, exp.Union, exp.Subquery)
    for t in trees:
        if not isinstance(t, allowed):
            return "syntax_error", f"语句无法识别（疑似拼写错误）：{s[:40]}"
        # 表名拼错（语法合法但执行必失败，等价于 ERROR 1146）
        if tables is not None:
            for tb in t.find_all(exp.Table):
                name = (tb.name or "").lower()
                schema = (tb.db or "").lower()
                if not name or schema in ("information_schema", "mysql",
                                          "performance_schema", "sys"):
                    continue
                if name not in tables:
                    return "unknown_table", f"表 {tb.name} 不存在（名字拼错或还没创建）"
        if isinstance(t, (exp.Update, exp.Delete)):
            if t.args.get("where") is None:
                verb = "UPDATE" if isinstance(t, exp.Update) else "DELETE"
                return "dangerous", f"{verb} 没有 WHERE 条件，会作用于整张表"
        if isinstance(t, exp.Select):
            for proj in t.expressions:
                if isinstance(proj, exp.Star):
                    return "hint", "SELECT * 会取出全部列，建议明确列出所需字段"
            for j in t.find_all(exp.Join):
                has_cond = j.args.get("on") is not None or j.args.get("using") is not None
                is_cross = (j.args.get("kind") or "").upper() == "CROSS"
                if not has_cond and not is_cross:
                    return "hint", "JOIN 缺少 ON 条件（会得到笛卡尔积）"
    return "ok", ""


# ---------------------------------------------------------------------------
# 汇总查询（教师端）
# ---------------------------------------------------------------------------
# 诊断结果 30s TTL 缓存（性能优化）：采集+sqlglot 解析是 CPU 操作，
# 教师反复点击或多教师同时看同一实验单时，30s 内直接复用上次结果。
_DIAG_CACHE: dict[int, tuple[float, dict]] = {}
_DIAG_TTL = 30.0


def lab_errors(lab_id: int, samples_per_student: int = 5, recent: int = 20) -> dict:
    """采集 + 汇总：每个学生的练习总量、问题分布、最近几条原始 SQL。"""
    import time

    hit = _DIAG_CACHE.get(lab_id)
    if hit and time.monotonic() - hit[0] < _DIAG_TTL:
        return hit[1]

    result = _lab_errors_impl(lab_id, samples_per_student, recent)
    _DIAG_CACHE[lab_id] = (time.monotonic(), result)
    return result


def _lab_errors_impl(lab_id: int, samples_per_student: int, recent: int) -> dict:
    models.get_lab(lab_id)  # 不存在 → NotFound
    collected = collect_lab(lab_id)
    conn = db.connect()
    try:
        agg = conn.execute(
            "SELECT student_id, verdict, COUNT(*) AS c FROM student_queries "
            "WHERE lab_id = ? GROUP BY student_id, verdict", (lab_id,)
        ).fetchall()
        problems = conn.execute(
            "SELECT student_id, ts, sql_text, verdict, detail FROM student_queries "
            "WHERE lab_id = ? AND verdict != 'ok' ORDER BY id DESC LIMIT 300", (lab_id,)
        ).fetchall()
    finally:
        conn.close()

    per: dict[str, dict] = {}
    for r in agg:
        d = per.setdefault(r["student_id"], {"student_id": r["student_id"],
                                             "total": 0, "ok": 0, "problems": 0,
                                             "by_verdict": {}, "samples": []})
        if r["verdict"] == "ok":
            d["ok"] += r["c"]
        else:
            d["problems"] += r["c"]
            d["by_verdict"][r["verdict"]] = r["c"]
        d["total"] += r["c"]

    # 样例按 SQL 文本去重：重复的同一句错（学生反复执行）不刷屏，
    # 教师看到的是"不同种类"的错误
    seen: dict[str, set] = {}
    for r in problems:
        d = per.get(r["student_id"])
        if not d or len(d["samples"]) >= samples_per_student:
            continue
        s = seen.setdefault(r["student_id"], set())
        if r["sql_text"] in s:
            continue
        s.add(r["sql_text"])
        d["samples"].append({"ts": r["ts"], "sql": r["sql_text"],
                             "verdict": r["verdict"], "detail": r["detail"]})

    students = sorted(per.values(), key=lambda x: (-x["problems"], x["student_id"]))
    return {
        "lab_id": lab_id,
        "collected": collected,
        "students": students,
        "recent": [dict(r) for r in problems[:recent]],
    }
