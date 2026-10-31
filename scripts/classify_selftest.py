"""classify() 判定规则自测（离线假 schema，验证列名检查有效且不误报）。

用法：python3.11 scripts/classify_selftest.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.insights import classify  # noqa: E402

TABLES = {"customer", "product", "orders"}
COLS = {
    "customer": {"cid", "cname", "city"},
    "product": {"pid", "pname", "price", "stock"},
    "orders": {"oid", "cid", "pid", "qty", "order_date"},
}

# (SQL, 期望 verdict, 是否带列结构上下文)
CASES = [
    ("SELECT * FROM customer;", "hint", True),
    ("SELECT cname, city FROM customer WHERE city = '深圳';", "ok", True),
    ("SELECT c.cname, o.oid FROM customer c JOIN orders o;", "hint", True),
    ("SELECT * FROM customerr;", "unknown_table", True),
    ("SELECT * FROM orders WHERE amount > 100;", "unknown_column", True),
    ("SELECT c.name FROM customer c;", "unknown_column", True),
    ("UPDATE product SET price = 0;", "dangerous", True),
    ("DELETE FROM orders;", "dangerous", True),
    ("SELECT o.oid, c.cname, p.pname FROM orders o "
     "JOIN customer c ON o.cid = c.cid "
     "JOIN product p ON o.pid = p.pid;", "ok", True),
    ("SELEC * FORM customer;", "syntax_error", True),
    # 别名引用（HAVING cnt / ORDER BY 别名）不能被当成"列不存在"
    ("SELECT COUNT(*) AS cnt FROM orders HAVING cnt > 1;", "ok", True),
    ("SELECT cname AS n FROM customer ORDER BY n;", "ok", True),
    # 拿不到结构时（columns=None）不得误判列名
    ("SELECT * FROM orders WHERE amount > 100;", "hint", False),
]

fail = 0
for sql, want, use_cols in CASES:
    got, detail = classify(sql, TABLES, COLS if use_cols else None)
    ok = got == want
    fail += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  期望 {want:14s} 实际 {got:14s} | {sql[:56]}")
    if not ok:
        print(f"       detail={detail}")

print(f"\n{len(CASES) - fail}/{len(CASES)} 通过")
sys.exit(1 if fail else 0)
