"""config.py — 全局配置中心。

原则：所有可调参数（路径、端口段、容器限额、密钥）只在这一处定义，
环境变量覆盖，其余模块一律 from . import config 引用。
新增任务 3（models/auth）、任务 8（部署）的配置也加在这里。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
LABS_DATA_DIR = DATA_DIR / "labs"            # 每实例数据卷: data/labs/<lab_id>/<student_id>/

# 元数据库：按交接文档第四节，6 张表共库（含 port_pool），不再分文件
DB_PATH = Path(os.environ.get("APP_DB_PATH", str(DATA_DIR / "app.db")))


def _load_dotenv() -> None:
    """极简 .env 加载（ROOT/.env，KEY=VALUE 格式，# 注释）。

    不引 python-dotenv 依赖；生产部署也可用 systemd Environment= 注入。
    """
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

# ---------------------------------------------------------------------------
# 元数据 / 密钥
# ---------------------------------------------------------------------------
# 学生密码对称加密密钥（任务 3 接入）；缺失时启动检查会告警
APP_SECRET = os.environ.get("APP_SECRET", "")

# ---------------------------------------------------------------------------
# 容器参数（交接文档第五节：照抄，别自己改）
# ---------------------------------------------------------------------------
MYSQL_IMAGE = os.environ.get("MYSQL_IMAGE", "mysql:8.0")

CPU_CORES = float(os.environ.get("LAB_CPU_CORES", "0.5"))
MEM_LIMIT = os.environ.get("LAB_MEM_LIMIT", "256m")
MEM_SWAP = os.environ.get("LAB_MEM_SWAP", "256m")   # 必须与 mem_limit 相同
PIDS_LIMIT = int(os.environ.get("LAB_PIDS_LIMIT", "200"))
LOG_MAX_SIZE = os.environ.get("LAB_LOG_MAX_SIZE", "10m")
LOG_MAX_FILE = os.environ.get("LAB_LOG_MAX_FILE", "2")
RESTART_POLICY = {"Name": "on-failure", "MaximumRetryCount": 3}

# --cap-drop ALL 后 mysql entrypoint chown 失败的最小补集（验证过）
MYSQL_REQUIRED_CAPS = ("CHOWN", "SETUID", "SETGID")

# 容器时区：学生 NOW()/CURDATE() 与北京时间一致
MYSQL_TZ = os.environ.get("MYSQL_TZ", "Asia/Shanghai")

# ---------------------------------------------------------------------------
# 端口池（交接文档第二节：33001-33100，闭区间）
# ---------------------------------------------------------------------------
PORT_RANGE_START = int(os.environ.get("PORT_RANGE_START", "33001"))
PORT_RANGE_END = int(os.environ.get("PORT_RANGE_END", "33100"))

# ---------------------------------------------------------------------------
# 批量创建（任务 4）
# ---------------------------------------------------------------------------
CREATE_CONCURRENCY = int(os.environ.get("LAB_CREATE_CONCURRENCY", "5"))  # 线程池上限
CREATE_RETRIES = int(os.environ.get("LAB_CREATE_RETRIES", "3"))          # 端口冲突重试次数
CREATE_WAIT_TIMEOUT = int(os.environ.get("LAB_CREATE_WAIT_TIMEOUT", "180"))  # 单容器健康等待(秒)
# 健康探测（学生账号真实查询）：5 并发初始化时 2 核 CPU 上 MySQL 首次初始化
# 可超 1 分钟，默认 10次×2s 必误判失败（实测坑 #9）→ 40次×3s = 2 分钟窗口
CREATE_PING_RETRIES = int(os.environ.get("LAB_PING_RETRIES", "40"))
CREATE_PING_INTERVAL = int(os.environ.get("LAB_PING_INTERVAL", "3"))

# CSV 导出中的主机地址（空则用请求的 Host）；正式部署设为服务器公网 IP 或域名
PUBLIC_HOST = os.environ.get("LAB_PUBLIC_HOST", "")


def ensure_dirs() -> None:
    """确保数据目录存在（模块加载不产生副作用，由入口显式调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LABS_DATA_DIR.mkdir(parents=True, exist_ok=True)


def check_startup() -> list[str]:
    """启动自检，返回告警列表（供任务 3 的 FastAPI lifespan 调用）。"""
    warnings: list[str] = []
    if not APP_SECRET:
        warnings.append("APP_SECRET 未设置：任务 3 接入密码加密前必须配置（生产 32+ 字节随机值）")
    return warnings
