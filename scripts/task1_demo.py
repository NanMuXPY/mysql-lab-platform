"""任务 1 验收脚本：建 1 个 MySQL 实例并验证可连接、初始数据已导入。

用法（工作区根目录执行）:
  python scripts/task1_demo.py            # 创建并验证
  python scripts/task1_demo.py --cleanup  # 验收后删除容器和数据卷

验收标准（交接文档第八节任务 1）:
  命令行建出的容器，Navicat 能连上并看到初始表。
  脚本最后打印的连接信息可直接填进 Navicat 人工复核。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app import docker_svc as svc  # noqa: E402

config.ensure_dirs()

LAB_ID = 1
STUDENT_ID = "stu_demo"
HOST_PORT = 33001
VOLUME_PATH = config.LABS_DATA_DIR / str(LAB_ID) / STUDENT_ID
INIT_SQL = ROOT / "templates_sql" / "sample_basic.sql"
CONTAINER = f"lab-{LAB_ID}-{STUDENT_ID}"
DB_NAME = "lab_demo"
DB_USER = "stu_demo"


def main() -> int:
    cleanup = "--cleanup" in sys.argv

    # 幂等：先清掉同名容器与旧数据卷，保证每次都是"干净数据卷 + 重跑 init SQL"
    print("[*] 预清理同名容器与旧数据卷（幂等重跑）")
    svc.remove_instance(CONTAINER)
    if VOLUME_PATH.exists():
        svc.clear_volume(str(VOLUME_PATH))

    if cleanup:
        print(f"[*] 删除容器 {CONTAINER} 与数据卷 {VOLUME_PATH}")
        svc.remove_instance(CONTAINER)
        svc.delete_volume(str(VOLUME_PATH))
        print("[OK] 已清理")
        return 0

    print(f"[*] 生成凭证（root 密码只在创建时可见，不入库）")
    cred = svc.generate_credentials()

    print(f"[*] 创建容器 {CONTAINER}: host 33001 -> mysql:3306, 卷 {VOLUME_PATH}")
    info = svc.create_mysql_instance(
        lab_id=LAB_ID,
        student_id=STUDENT_ID,
        host_port=HOST_PORT,
        volume_path=str(VOLUME_PATH),
        init_sql_path=str(INIT_SQL),
        db_name=DB_NAME,
        db_user=DB_USER,
        db_pass=cred["db_pass"],
        root_password=cred["root_password"],
    )
    print(f"    container_id = {info['container_id'][:12]}, status = {info['status']}")

    print("[*] 等待初始化完成（首次启动约 20-40 秒）...")
    ready = svc.wait_until_running(CONTAINER, timeout=180)
    print(f"    已就绪: {ready['host']}:{ready['port']}")

    status = svc.get_instance_status(CONTAINER)
    print(f"[*] 容器状态: {status['status']}")

    print(f"[*] 用学生账号 {DB_USER} 做真实查询验证 ...")
    svc.ping_mysql(CONTAINER, DB_USER, cred["db_pass"], DB_NAME)
    print("[OK] 学生账号可查询自己的库")

    print("[*] 初始数据行数验证（应各 3 行）...")
    import docker
    c = docker.from_env().containers.get(CONTAINER)
    for table in ("student", "course", "score"):
        r = c.exec_run(
            ["mysql", "-h127.0.0.1", f"-u{DB_USER}", f"-p{cred['db_pass']}", "-D", DB_NAME,
             "-N", "-e", f"SELECT COUNT(*) FROM {table};"]
        )
        print(f"    {table:<8} rows = {r.output.decode().strip()}")

    print("\n========== Navicat 连接信息（发给学生的样式） ==========")
    print(f"  主机: 127.0.0.1（公网部署时填服务器 IP）")
    print(f"  端口: {ready['port']}")
    print(f"  库名: {DB_NAME}")
    print(f"  账号: {DB_USER}")
    print(f"  密码: {cred['db_pass']}")
    print("=======================================================")
    print("[提示] 验收通过后运行 python scripts/task1_demo.py --cleanup 清理")
    return 0


if __name__ == "__main__":
    sys.exit(main())
