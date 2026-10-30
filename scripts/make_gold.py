"""make_gold.py — 生成金镜像 datadir（性能优化，一次性执行）。

原理：让 mysql:8.0 空跑一次完整初始化（产出系统表 ~180MB），固化为宿主机
模板；此后每次建实例直接拷贝模板（跳过 mysqld --initialize，该步骤占
创建耗时 ~90%），单实例就绪 85s → ~15s。

产物：
  data/gold/mysql-8.0/      预初始化 datadir（仅系统表，无业务库无用户）
  data/gold/root.password   金镜像 root 密码（平台自留，bootstrap 时会替换）

用法（服务器上）：python3.11 scripts/make_gold.py [--force]
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, docker_svc  # noqa: E402
import docker  # noqa: E402


def main() -> int:
    force = "--force" in sys.argv
    gold = config.GOLD_DIR
    if docker_svc.gold_ready() and not force:
        print(f"金镜像已存在: {gold}（--force 重建）")
        return 0

    build_dir = gold.parent / "_build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    pwd = docker_svc.generate_credentials()["root_password"]
    cli = docker.from_env()
    print("[1/3] 启动临时容器做完整初始化（2核机约 1 分钟）…")
    t0 = time.time()
    c = cli.containers.run(
        config.MYSQL_IMAGE, name="gold-build", detach=True,
        environment={"TZ": config.MYSQL_TZ, "MYSQL_ROOT_PASSWORD": pwd},
        volumes={str(build_dir): {"bind": "/var/lib/mysql", "mode": "rw"}},
        cap_drop=["ALL"], cap_add=list(config.MYSQL_REQUIRED_CAPS),
    )
    # 就绪判定：容器 running 且 mysqladmin ping 通（真实服务已起）
    while True:
        c.reload()
        if c.status == "exited":
            print(c.logs(tail=40).decode("utf-8", "replace"))
            raise SystemExit("初始化容器异常退出，详见上方日志")
        if c.status == "running":
            r = c.exec_run(["mysqladmin", "-uroot", f"-p{pwd}", "ping"])
            if r.exit_code == 0:
                break
        time.sleep(2)
    print(f"      初始化完成，耗时 {time.time()-t0:.0f}s")

    print("[2/3] 停容器、固化模板…")
    c.stop(timeout=20)
    c.remove()

    if gold.exists():
        shutil.rmtree(gold)
    build_dir.rename(gold)
    config.GOLD_ROOT_PASSWORD_FILE.write_text(pwd, encoding="utf-8")

    size = sum(f.stat().st_size for f in gold.rglob("*") if f.is_file())
    print(f"[3/3] 完成: {gold}")
    print(f"      datadir {size/1048576:.0f} MiB，root 密码已存 "
          f"{config.GOLD_ROOT_PASSWORD_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
