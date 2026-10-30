"""docker_svc.py — 全平台唯一允许 import docker 的模块。

按《设计方案》第七节安全约束第 6 条：创建实例抽象为 provider 接口，
将来换 KVM / 轻量云主机只换实现，上层（labs.py）不动。

职责边界：
- 只管"单个容器的建/查/删/健康检查"原语，不管端口（ports.py）、不管编排（labs.py）
- 所有参数来自 config，异常统一抛 errors.ProviderError

已知坑（交接文档第五节 + 实测）：
- 初始化 SQL 只在数据卷为空时执行一次 → 重置 = 删容器 + 清空卷 + 重建
- --cap-drop ALL 会让 entrypoint chown 失败 → 补最小 cap 集合
- 容器内 mysql 客户端必须 -h127.0.0.1（默认走 socket 连不上）
- entrypoint"临时服务→正式服务"切换有 1-2 秒连接空窗，健康检查要重试
"""

from __future__ import annotations

import os
import secrets
import shutil
import string
from typing import Optional

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from . import config
from .errors import ProviderError  # noqa: F401  (re-export，上层统一 from errors 捕获)

# ---------------------------------------------------------------------------
# 凭证
# ---------------------------------------------------------------------------
def _rand_password(n: int = 16) -> str:
    """16 位随机强密码（字母+数字，避开 shell/URL 敏感字符）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def generate_credentials() -> dict:
    """生成一套实例凭证。root 密码只有平台知道，从不发给学生。"""
    return {"root_password": _rand_password(), "db_pass": _rand_password()}


def _client() -> docker.DockerClient:
    """每次取客户端：避免进程内长连接在 Docker 重启后失效。"""
    return docker.from_env()


# ---------------------------------------------------------------------------
# Provider 接口：上层只认识这几个方法
# ---------------------------------------------------------------------------
def create_mysql_instance(
    lab_id: int | str,
    student_id: str,
    host_port: int,
    volume_path: str,
    init_sql_path: str,
    db_name: str,
    db_user: str,
    db_pass: str,
    root_password: str,
) -> dict:
    """创建一个学生的独立 MySQL 容器。

    成功：返回 {"container_id", "container_name", "status": "creating"}
          （容器刚起还在跑 entrypoint 初始化，到 running 由 get_instance_status 轮询确认）
    失败：抛 ProviderError，调用方必须回滚端口租约并写 last_error。
          本函数已兜底清理半创建的容器，避免 docker ps -a 残留。
    """
    container_name = f"lab-{lab_id}-{student_id}"
    volume_path = os.path.abspath(volume_path)
    init_sql_path = os.path.abspath(init_sql_path)

    if not os.path.isfile(init_sql_path):
        raise ProviderError(f"init SQL 不存在: {init_sql_path}")

    # 数据卷目录预创建：bind mount 下 entrypoint 自行 chown
    os.makedirs(volume_path, exist_ok=True)

    cli = _client()
    try:
        try:
            cli.images.get(config.MYSQL_IMAGE)
        except ImageNotFound:
            raise ProviderError(
                f"镜像 {config.MYSQL_IMAGE} 不存在，请先 docker pull {config.MYSQL_IMAGE}"
            )

        container = cli.containers.run(
            config.MYSQL_IMAGE,
            name=container_name,
            detach=True,
            # ---- 资源限额（防学生拖垮主机）----
            nano_cpus=int(config.CPU_CORES * 1e9),
            mem_limit=config.MEM_LIMIT,
            memswap_limit=config.MEM_SWAP,
            pids_limit=config.PIDS_LIMIT,
            # ---- 安全收敛 ----
            cap_drop=["ALL"],
            cap_add=list(config.MYSQL_REQUIRED_CAPS),
            security_opt=["no-new-privileges"],
            # ---- 日志轮转（防磁盘写满，教学平台最常见事故）----
            log_config=docker.types.LogConfig(
                type="json-file",
                config={"max-size": config.LOG_MAX_SIZE, "max-file": config.LOG_MAX_FILE},
            ),
            restart_policy=config.RESTART_POLICY,
            # ---- 挂载 ----
            volumes={
                volume_path: {"bind": "/var/lib/mysql", "mode": "rw"},
                init_sql_path: {
                    "bind": "/docker-entrypoint-initdb.d/01-init.sql",
                    "mode": "ro",
                },
            },
            ports={"3306/tcp": host_port},
            # ---- 凭证（root 密码平台自留；MYSQL_USER 只对自己库有权限）----
            environment={
                "TZ": config.MYSQL_TZ,
                "MYSQL_ROOT_PASSWORD": root_password,
                "MYSQL_DATABASE": db_name,
                "MYSQL_USER": db_user,
                "MYSQL_PASSWORD": db_pass,
            },
            # ---- mysqld 调优（照抄交接文档第五节）----
            command=[
                "--innodb-buffer-pool-size=64M",
                "--performance-schema=OFF",
                "--max-connect-errors=10",
                "--character-set-server=utf8mb4",
                "--collation-server=utf8mb4_unicode_ci",
                # ---- 学习分析：general_log 记录学生每条 SQL（含失败的），
                #      写进数据卷由 insights.py 增量采集（P1 错题诊断）----
                "--log-output=FILE",
                "--general-log=ON",
                "--general-log-file=/var/lib/mysql/general.log",
            ],
        )
        return {
            "container_id": container.id,
            "container_name": container_name,
            "status": "creating",
        }
    except APIError as e:
        # 兜底清理半创建的容器（409/500 等都可能留下残骸）
        try:
            cli.containers.get(container_name).remove(force=True)
        except Exception:
            pass
        raise ProviderError(f"Docker API 创建失败: {e}") from e


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
_STATUS_MAP = {
    "created": "creating",
    "running": "running",
    "restarting": "running",
    "paused": "stopped",
    "exited": "stopped",
    "dead": "failed",
    "removing": "stopped",
}


def get_instance_status(container_name: str) -> Optional[dict]:
    """查单个实例状态。容器不存在返回 None（调用方可判定 recycled）。

    返回: {"container_id", "status", "started_at", "exit_code", "last_error"}
    status: creating | running | stopped | failed（与 instances 表状态机一致）
    """
    try:
        c = _client().containers.get(container_name)
    except NotFound:
        return None
    except APIError as e:
        raise ProviderError(f"Docker API 查询失败: {e}") from e

    state = c.attrs.get("State", {})
    mapped = _STATUS_MAP.get(c.status, "failed")
    exit_code = state.get("ExitCode")

    last_error = None
    if mapped == "stopped" and exit_code not in (0, None):
        mapped = "failed"
        last_error = f"容器异常退出 exit_code={exit_code}"
    elif mapped == "failed":
        last_error = f"容器状态异常: {c.status}"

    return {
        "container_id": c.id,
        "status": mapped,
        "started_at": state.get("StartedAt"),
        "exit_code": exit_code,
        "last_error": last_error,
    }


def wait_until_running(container_name: str, timeout: int = 120) -> dict:
    """阻塞等待容器进入可服务状态（冒烟脚本用；正式链路由前端轮询驱动）。

    判定：State=running 且 TCP 可连。注意与 ping_mysql 的重试配合，
    规避 entrypoint 切换期的 1-2 秒连接空窗。
    """
    import socket
    import time

    cli = _client()
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            c = cli.containers.get(container_name)
        except NotFound:
            raise ProviderError("容器在等待期间消失了")
        c.reload()
        last_status = c.status
        if c.status == "exited":
            tail = "\n".join(c.logs(tail=30).decode("utf-8", "replace").splitlines()[-10:])
            raise ProviderError(f"容器初始化失败退出:\n{tail}")
        if c.status == "running":
            port_map = c.attrs["NetworkSettings"]["Ports"]["3306/tcp"]
            host = port_map[0]["HostIp"]
            host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
            sport = int(port_map[0]["HostPort"])
            with socket.socket() as s:
                s.settimeout(2)
                if s.connect_ex((host, sport)) == 0:
                    return {"ready": True, "host": host, "port": sport}
        time.sleep(2)
    raise ProviderError(f"等待超时（{timeout}s），容器最后状态: {last_status}")


def ping_mysql(container_name: str, user: str, password: str, db: str,
               retries: int = 10, interval: int = 2) -> bool:
    """用学生账号在容器内执行真实查询，验证账号/库/权限链路（带重试）。"""
    import time

    last_err, exit_code = None, None
    for _ in range(retries):
        c = _client().containers.get(container_name)
        r = c.exec_run(
            ["mysql", "-h127.0.0.1", f"-u{user}", f"-p{password}", "-D", db,
             "-e", "SELECT 1;"]
        )
        if r.exit_code == 0:
            return True
        exit_code = r.exit_code
        last_err = r.output.decode("utf-8", "replace")[:300]
        time.sleep(interval)
    raise ProviderError(
        f"ping 失败（重试 {retries} 次）exit={exit_code}: {last_err}"
    )


# ---------------------------------------------------------------------------
# 销毁
# ---------------------------------------------------------------------------
def remove_instance(container_name: str) -> None:
    """删除容器（不动数据卷）。重置 = remove_instance + clear_volume + create。"""
    try:
        c = _client().containers.get(container_name)
    except NotFound:
        return  # 幂等
    except APIError as e:
        raise ProviderError(f"Docker API 删除失败: {e}") from e
    c.remove(force=True)


def clear_volume(volume_path: str) -> None:
    """清空数据卷目录。重置时必须清，否则 init SQL 不会重跑（已知坑）。"""
    volume_path = os.path.abspath(volume_path)
    if os.path.isdir(volume_path):
        shutil.rmtree(volume_path)
    os.makedirs(volume_path, exist_ok=True)


def delete_volume(volume_path: str) -> None:
    """彻底删除数据卷目录（回收流程，释放磁盘）。"""
    volume_path = os.path.abspath(volume_path)
    if os.path.isdir(volume_path):
        shutil.rmtree(volume_path)
