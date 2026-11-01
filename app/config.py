"""config.py — 全局配置中心。

原则：所有可调参数（路径、端口段、容器限额、密钥）只在这一处定义，
环境变量覆盖，其余模块一律 from . import config 引用。
新增任务 3（models/auth）、任务 8（部署）的配置也加在这里。
"""

from __future__ import annotations

import os
import secrets
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


def _ensure_secret() -> str:
    """APP_SECRET 缺失时自动生成并落盘到 .env（首次启动自举）。

    为什么不直接报错退出：这是给老师用的教学平台，"克隆代码 + 起服务"就该能跑，
    让使用者先手工造一个密钥是没必要的门槛。
    但密钥**必须落盘**：它是学生数据库密码的解密密钥，重启后若换了一把，
    所有已加密的密码就全解不开了。所以写进 .env（600 权限）并提示要备份。
    """
    if os.environ.get("APP_SECRET"):
        return os.environ["APP_SECRET"]
    secret = secrets.token_hex(32)
    try:
        env_file = ROOT / ".env"
        with env_file.open("a", encoding="utf-8") as f:
            f.write(f"\nAPP_SECRET={secret}\n")
        env_file.chmod(0o600)
    except OSError as e:  # 只读目录等：退化为仅本进程有效，启动检查会提示
        print(f"[config] 无法写入 .env（{e}），APP_SECRET 仅本进程有效")
    os.environ["APP_SECRET"] = secret
    return secret


# 是否来自 .env / 环境变量（false 说明是本次自举生成的，需提示备份）
_SECRET_PREEXISTING = bool(os.environ.get("APP_SECRET"))

# ---------------------------------------------------------------------------
# 元数据 / 密钥
# ---------------------------------------------------------------------------
# 学生密码对称加密密钥（任务 3 接入）
APP_SECRET = _ensure_secret()

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
# 必须是 unless-stopped，不能用 on-failure：
#   系统关机时 mysqld 收到 SIGTERM 正常退出（ExitCode=0），on-failure 只重启
#   非 0 退出，所以服务器一重启，全班实例全部静默停掉，而元数据仍是 running。
#   unless-stopped 的语义正好：机器重来自动拉起，教师手动 docker stop 则不拉起。
RESTART_POLICY = {"Name": "unless-stopped"}

# --cap-drop ALL 后 mysql entrypoint 正常工作所需的最小补集（每项都实测过，别乱加）
#   CHOWN / SETUID / SETGID —— entrypoint 要 chown datadir 并降到 mysql 用户运行
#   DAC_OVERRIDE —— 关键：容器内 root 要能读属主为 mysql、权限 750 的库目录。
#     少了它，**首次启动正常，一旦 datadir 里有了真实的库目录，重启就必然崩溃**：
#     entrypoint 启动时遍历 datadir 会报 "find: /var/lib/mysql/<db>: Permission denied"
#     然后 exit 1，容器陷入 restarting 循环。这个坑只在重启后才显形，极易漏测。
MYSQL_REQUIRED_CAPS = ("CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE")

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

# ---------------------------------------------------------------------------
# 金镜像（性能优化：跳过 mysqld --initialize，单实例就绪 85s → ~15s）
# ---------------------------------------------------------------------------
# 预初始化好的系统表 datadir 模板（由 scripts/make_gold.py 一次性生成）。
# 原理：entrypoint 见 datadir 非空会跳过 mysqld --initialize（创建耗时的大头），
#       但也跳过 initdb.d 脚本 → 由 bootstrap_instance() 在启动后一次性
#       建库/建号/授权/灌模板 SQL。金镜像缺失或 bootstrap 失败自动回退慢路径。
GOLD_DIR = Path(os.environ.get("LAB_GOLD_DIR", str(DATA_DIR / "gold" / "mysql-8.0")))
GOLD_ROOT_PASSWORD_FILE = GOLD_DIR.parent / "root.password"


def ensure_dirs() -> None:
    """确保数据目录存在（模块加载不产生副作用，由入口显式调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LABS_DATA_DIR.mkdir(parents=True, exist_ok=True)


# MySQL 8.0 每个实例启动时通过 io_setup() 向内核申请约 2700 个 AIO 事件。
# 默认 fs.aio-max-nr=65536 → 只能容纳 24 个实例，第 25 个起 mysqld 直接起不来，
# 表现为容器 "is not running / is restarting"，和内存、端口、磁盘都无关，极难排查。
# （4核8G 实测：50 个实例只有 24 个成功，调大该值后 44 个成功）
AIO_PER_INSTANCE = 2700
AIO_RECOMMENDED = 1048576


def _aio_warning() -> str | None:
    """按端口池规划容量反查内核 AIO 上限是否够用。"""
    try:
        with open("/proc/sys/fs/aio-max-nr", encoding="utf-8") as f:
            limit = int(f.read().strip())
    except (OSError, ValueError):
        return None  # 非 Linux 或不可读：不做判断
    capacity = limit // AIO_PER_INSTANCE
    slots = PORT_RANGE_END - PORT_RANGE_START + 1
    if capacity < slots:
        return (f"内核 AIO 事件上限偏低：fs.aio-max-nr={limit}，每个 MySQL 实例约需 "
                f"{AIO_PER_INSTANCE}，当前只能承载 {capacity} 个实例"
                f"（端口池规划 {slots} 个）。第 {capacity + 1} 个起容器会反复重启。"
                f"修复：sysctl -w fs.aio-max-nr={AIO_RECOMMENDED}"
                f"（并写入 /etc/sysctl.d/99-mysql-lab.conf 持久化）")
    return None


def check_startup() -> list[str]:
    """启动自检，返回告警列表（供任务 3 的 FastAPI lifespan 调用）。"""
    warnings: list[str] = []
    if not _SECRET_PREEXISTING:
        warnings.append("APP_SECRET 未预置，已自动生成并写入 .env："
                        "务必与 data/app.db 一起备份，丢失后学生密码将无法解密")
    aio = _aio_warning()
    if aio:
        warnings.append(aio)
    return warnings
