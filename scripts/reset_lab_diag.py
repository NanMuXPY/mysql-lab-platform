"""reset_lab_diag.py — 清空某实验单的 SQL 采集记录并重设基线（调试/演示用）。

用于：判定规则改动后，希望用同一批实例重新生成一份干净的"学生练习"数据，
而不必重建容器。

用法：python3.11 scripts/reset_lab_diag.py <lab_id>
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, insights  # noqa: E402

lab_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1

conn = db.connect()
try:
    insts = conn.execute(
        "SELECT container_name, volume_path FROM instances WHERE lab_id = ?",
        (lab_id,),
    ).fetchall()
finally:
    conn.close()

with db.write() as c:
    c.execute("DELETE FROM student_queries WHERE lab_id = ?", (lab_id,))

for i in insts:
    insights.set_baseline(i["container_name"], i["volume_path"])

print(f"实验单 {lab_id}: 已清空采集记录并对 {len(insts)} 个实例重设基线")
