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
from datetime import datetime, timedelta, timezone

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from . import crypto, db, models

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
    # 平台自己为了做表/列名校验而发的元数据查询（会同样写进 general_log，
    # 不挡掉就会被下一轮采集当成"学生 SQL"，全是误报）
    "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA",
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
def collect_lab(lab_id: int, container: str | None = None) -> dict:
    """增量采集一个实验单全部实例的新增 SQL，返回本轮统计。

    每个实例额外查一次"库里有哪些表"，用于识别"表名拼错"这类常见失误
    （语法合法但执行会失败 —— 1064 纯语法错 MySQL 不写 general log，抓不到）。

    container 非空时只采该实例：学生端「我的错题」每次刷新都采一次，
    若每次都扫全班 20+ 容器（每个还要 docker exec 查 schema），
    20 个学生同时刷新就是 400 次 exec —— 只采自己那一个是 O(1)。
    """
    conn = db.connect()
    try:
        sql = ("SELECT id, student_id, container_name, volume_path, db_name, "
               "db_user, db_pass_enc FROM instances "
               "WHERE lab_id = ? AND status IN ('running','stopped')")
        params: list = [lab_id]
        if container:
            sql += " AND container_name = ?"
            params.append(container)
        instances = conn.execute(sql, params).fetchall()
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
        schema = None
        try:
            schema = _instance_schema(inst["container_name"], inst["db_name"],
                                      inst["db_user"],
                                      crypto.decrypt(inst["db_pass_enc"]))
        except Exception as e:  # 解密失败（老数据/密钥变更）→ 不校验
            log.debug("实例 %s 凭据解密失败: %s", inst["container_name"], e)
        tables, columns = (schema if schema else (None, None))
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
            text = body.decode("utf-8", "replace")
            # 本批里有 DDL（学生自己建表/改表）时，库结构正在变，
            # 拿采集前的快照校验列名必然误报 → 本批跳过列检查
            cols_arg = None if re.search(
                r"\b(CREATE|ALTER|DROP)\s+TABLE\b", text, re.I) else columns
            for line in text.splitlines():
                row = _parse_line(line, lab_id, inst["container_name"],
                                  inst["student_id"], now, tables, cols_arg)
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


def _instance_schema(container: str, db_name: str, db_user: str, db_pass: str
                     ) -> tuple[set[str], dict[str, set[str]]] | None:
    """取学生库的 (表名集合, {表: 列集合})，一次 information_schema 查询搞定。

    用**学生账号**查而不是 root：权限最小（information_schema 只会返回他有权限
    的对象，天然限定在自己的 lab_ 库），也不依赖 root 密码在各条创建路径上一致。
    失败返回 None —— 调用方跳过表名/列名校验，宁可漏判也不误判。
    """
    import subprocess
    try:
        sql = ("SELECT table_name, column_name FROM information_schema.columns "
               f"WHERE table_schema='{db_name}'")
        r = subprocess.run(
            ["docker", "exec", container, "mysql", "-h127.0.0.1",
             f"-u{db_user}", f"-p{db_pass}", "-N", "-B", "-e", sql],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log.debug("取表结构失败 %s: %s", container,
                      (r.stderr or "").strip()[-160:])
            return None
        cols: dict[str, set[str]] = {}
        for ln in r.stdout.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2 and parts[0].strip():
                cols.setdefault(parts[0].strip().lower(), set()).add(
                    parts[1].strip().lower())
        return (set(cols), cols) if cols else None
    except Exception as e:  # 容器不在/超时等：降级为不校验
        log.debug("取表结构异常 %s: %s", container, e)
        return None


def _parse_line(line: str, lab_id: int, instance_id: str, student_id: str,
                now: str, tables: set[str] | None = None,
                columns: dict[str, set[str]] | None = None) -> tuple | None:
    m = _LINE_RE.match(line)
    if not m or m.group("cmd") != "Query":
        return None  # 只关心 Query（Connect/Quit 等忽略）
    sql = (m.group("arg") or "").strip()
    if not sql:
        return None
    verdict, detail = classify(sql, tables, columns)
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
def classify(sql: str, tables: set[str] | None = None,
             columns: dict[str, set[str]] | None = None) -> tuple[str, str]:
    """返回 (verdict, detail)。

    verdict: ok | syntax_error | unknown_table | unknown_column | dangerous | hint | noise
    tables: 学生库现有表名集合（小写）；提供时启用"表名拼错"校验。
    columns: {表名: 列名集合}；提供时启用"列名拼错"校验（等价于 ERROR 1054）。

    判定顺序刻意对齐 MySQL 执行时的报错顺序（语法 → 表 → 列 → 语义警告），
    这样教师看到的判定和学生屏幕上实际弹出的报错是同一件事。
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
                db_part = (tb.db or "").lower()
                if not name or db_part in ("information_schema", "mysql",
                                           "performance_schema", "sys"):
                    continue
                if name not in tables:
                    return "unknown_table", f"表 {tb.name} 不存在（名字拼错或还没创建）"
        # 列名拼错（等价于 ERROR 1054）：库里所有表的列名集合都命中不了 → 必错。
        # 只看"整库都不存在"，不做单表归属判断，避免多表 JOIN/别名场景误报。
        if columns:
            all_cols = set().union(*columns.values())
            aliases = {(a.alias or "").lower() for a in t.find_all(exp.Alias)}
            aliases.discard("")
            for col in t.find_all(exp.Column):
                cname = (col.name or "").lower()
                if not cname or cname == "*" or cname in all_cols or cname in aliases:
                    continue
                return "unknown_column", f"列 {col.name} 不存在（字段名拼错？）"
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


def lab_errors(lab_id: int, samples_per_student: int = 5, recent: int = 20,
               timeline_per_student: int = 15, refresh: bool = False) -> dict:
    """采集 + 汇总：每个学生的练习量、问题分布、最近几条原始 SQL、**命令时间线**。

    refresh=True 跳过缓存（教师手动点诊断时要看最新内容）。
    """
    import time

    hit = _DIAG_CACHE.get(lab_id)
    if hit and not refresh and time.monotonic() - hit[0] < _DIAG_TTL:
        return hit[1]

    result = _lab_errors_impl(lab_id, samples_per_student, recent,
                              timeline_per_student)
    _DIAG_CACHE[lab_id] = (time.monotonic(), result)
    return result


# 教学场景固定东八区：主机时区可能是 UTC（云服务器默认），
# 直接用 astimezone() 会让教师看到的"14:32 敲的命令"实际是 06:32
_CST = timezone(timedelta(hours=8))


def _fmt_ts(ts: str) -> str:
    """general_log 的 UTC 时间戳 → 北京时间 HH:MM:SS（解析失败则截字符串兜底）。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CST).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return ts[11:19] if len(ts) >= 19 else ts


def _lab_errors_impl(lab_id: int, samples_per_student: int, recent: int,
                     timeline_per_student: int = 15) -> dict:
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
        # 每个学生的最近 N 条「原样命令」（含执行正确的），用于教师端逐人回放表格。
        # 用窗口函数按学生分组取，避免"全班最近 X 条"把安静的学生截空。
        tl_rows = conn.execute(
            "SELECT student_id, ts, sql_text, verdict, detail FROM ("
            "  SELECT student_id, ts, sql_text, verdict, detail,"
            "         ROW_NUMBER() OVER (PARTITION BY student_id ORDER BY id DESC) rn"
            "  FROM student_queries WHERE lab_id = ?"
            ") WHERE rn <= ? ORDER BY student_id, rn DESC",
            (lab_id, timeline_per_student),
        ).fetchall()
    finally:
        conn.close()

    per: dict[str, dict] = {}
    for r in agg:
        d = per.setdefault(r["student_id"], {"student_id": r["student_id"],
                                             "total": 0, "ok": 0, "problems": 0,
                                             "by_verdict": {}, "samples": [],
                                             "timeline": []})
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

    # 命令时间线：不去重、含正确语句，按时间正序（终端回放的阅读顺序）
    for r in tl_rows:
        d = per.get(r["student_id"])
        if not d:
            continue
        d["timeline"].append({"t": _fmt_ts(r["ts"]), "sql": r["sql_text"],
                              "verdict": r["verdict"], "detail": r["detail"]})

    students = sorted(per.values(), key=lambda x: (-x["problems"], x["student_id"]))
    return {
        "lab_id": lab_id,
        "collected": collected,
        "students": students,
        "recent": [dict(r) for r in problems[:recent]],
        "timeline_per_student": timeline_per_student,
    }


# ---------------------------------------------------------------------------
# 练习报告（教师导出 CSV）与学生自查视图（学习闭环）
# ---------------------------------------------------------------------------
_LABEL = {"syntax_error": "语法错误", "unknown_table": "表不存在",
          "unknown_column": "列不存在", "dangerous": "危险操作", "hint": "写法提示"}

# 建议刻意写成"下一步该做什么"，不是复述错误——学生看到"语法错误"四个字仍然不会改
_ADVICE = {
    "syntax_error": "检查关键字拼写、括号与引号是否成对、语句末尾是否多了逗号",
    "unknown_table": "先 SHOW TABLES 看库里有哪些表，再对照表名逐字核对",
    "unknown_column": "用 DESC 表名 查看字段；字段名不要加引号，别名要先在 SELECT 里定义",
    "dangerous": "UPDATE/DELETE 必须带 WHERE，否则整张表的行都会被改掉/删光",
    "hint": "SELECT * 改成明确列出需要的列；JOIN 必须带 ON 条件，否则得到的是笛卡尔积",
}


def report_rows(lab_id: int) -> list[dict]:
    """练习报告：每个学生一行（教师导出 CSV，也是期末平时分的客观依据）。

    以**实例名单**为基准而不是以"有练习记录的人"为基准：
    班里 30 人只有 5 人练过时，报告必须列出 30 行，其中 25 行练习数为 0——
    "谁根本没开始"恰恰是教师最需要看到的信息，不能被 join 掉。
    """
    data = lab_errors(lab_id, refresh=True)
    conn = db.connect()
    try:
        insts = conn.execute(
            "SELECT student_id, status FROM instances "
            "WHERE lab_id = ? AND status != 'recycled' ORDER BY student_id",
            (lab_id,)).fetchall()
        # 最后活跃时间与"最近一条问题命令"单独查：lab_errors 的 timeline 只保留
        # 最近 N 条，用它反推"最后一次"在练习量大的班级会不准
        last_ts = {r["student_id"]: r["ts"] for r in conn.execute(
            "SELECT student_id, MAX(ts) AS ts FROM student_queries "
            "WHERE lab_id = ? GROUP BY student_id", (lab_id,))}
        last_bad: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT student_id, sql_text, detail FROM ("
            "  SELECT student_id, sql_text, detail, ROW_NUMBER() OVER ("
            "    PARTITION BY student_id ORDER BY id DESC) rn"
            "  FROM student_queries WHERE lab_id = ? AND verdict != 'ok'"
            ") WHERE rn = 1", (lab_id,)):
            last_bad[r["student_id"]] = dict(r)
    finally:
        conn.close()

    stats = {s["student_id"]: s for s in data["students"]}
    roster = [(r["student_id"], r["status"]) for r in insts]
    # 万一个别学生有练习记录但实例已不在（重置/换机），也别丢他的数据
    for sid in stats:
        if all(sid != x[0] for x in roster):
            roster.append((sid, "recycled"))

    rows = []
    for sid, inst_status in roster:
        s = stats.get(sid)
        total = (s["total"] if s else 0) or 0
        bv = s["by_verdict"] if s else {}
        bad = last_bad.get(sid, {})
        rows.append({
            "student_id": sid,
            "instance_status": inst_status,
            "total": total,
            "ok": s["ok"] if s else 0,
            "problems": s["problems"] if s else 0,
            "syntax_error": bv.get("syntax_error", 0),
            "unknown_table": bv.get("unknown_table", 0),
            "unknown_column": bv.get("unknown_column", 0),
            "dangerous": bv.get("dangerous", 0),
            "hint": bv.get("hint", 0),
            "accuracy": round((s["ok"] if s else 0) * 100.0 / total, 1) if total else 0.0,
            "last_active": _fmt_ts(last_ts[sid]) if sid in last_ts else "",
            "last_problem_sql": bad.get("sql_text", ""),
            "last_problem_detail": bad.get("detail", ""),
        })
    return rows


def student_detail(lab_id: int, student_id: str, container: str,
                   limit: int = 40) -> dict:
    """学生自查视图：我的练习统计 + 我的命令回放（时间正序）+ 错因与改法。"""
    collect_lab(lab_id, container=container)
    conn = db.connect()
    try:
        agg = conn.execute(
            "SELECT verdict, COUNT(*) AS c FROM student_queries "
            "WHERE lab_id = ? AND student_id = ? GROUP BY verdict",
            (lab_id, student_id)).fetchall()
        rows = conn.execute(
            "SELECT ts, sql_text, verdict, detail FROM student_queries "
            "WHERE lab_id = ? AND student_id = ? ORDER BY id DESC LIMIT ?",
            (lab_id, student_id, limit)).fetchall()
    finally:
        conn.close()

    by = {r["verdict"]: r["c"] for r in agg}
    total = sum(by.values())
    ok = by.get("ok", 0)
    tips = [{"verdict": v, "label": _LABEL.get(v, v), "count": c,
             "advice": _ADVICE.get(v, "")}
            for v, c in sorted(by.items(), key=lambda kv: -kv[1]) if v != "ok"]
    return {
        "student_id": student_id,
        "total": total,
        "ok": ok,
        "problems": total - ok,
        "accuracy": round(ok * 100.0 / total, 1) if total else 0.0,
        "tips": tips,
        "timeline": [{"t": _fmt_ts(r["ts"]), "sql": r["sql_text"],
                      "verdict": r["verdict"], "detail": r["detail"]}
                     for r in reversed(rows)],
        "shown": min(len(rows), limit),
    }
