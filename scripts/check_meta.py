"""重启后元数据完整性检查（任务 8 验收用，一次性脚本）。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

c = db.connect()
for table in ("labs", "instances", "users", "templates", "audit_logs"):
    n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"{table}: {n}")
print("recycled labs:", c.execute(
    "SELECT COUNT(*) FROM labs WHERE status='recycled'").fetchone()[0])
