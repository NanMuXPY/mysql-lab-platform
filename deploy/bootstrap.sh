#!/bin/bash
# deploy/bootstrap.sh — 云服务器冷启动脚本（v1.0.3）
#
# 用途（两种，同一个脚本）：
#   1. 做自定义镜像的原料：买一台最小按量 ECS，user-data 注入本脚本，
#      跑完验证后「创建自定义镜像」——之后每次开课用该镜像开机，约 1 分钟服务就绪
#   2. 无镜像时的冷启动：任何新的 Anolis 3 / Ubuntu 24.04 机器，一条命令装到能开课
#
# 用法（阿里云控制台：ECS 实例自定义数据注入；或 SSH 后手动执行）：
#   sudo bash bootstrap.sh                 # 基础版：装环境 + 起服务
#   sudo WITH_GOLD=1 bash bootstrap.sh     # 顺带生成 MySQL 金镜像（+约 40s，强烈建议做镜像前带上）
#   sudo REPO_URL=<你的仓库> bash bootstrap.sh   # 机器上没有代码时，从 git 拉取
#
# 幂等：可重复执行，已完成的步骤自动跳过。可安全用于重试。
#
# 铁律：本脚本不写入任何密钥。APP_SECRET 由应用首次启动自动生成（600 权限），
#       "用完即释放"场景下每台机器一套新密钥，学生实例密码随元数据一起全新，互不影响。

set -euo pipefail

log() { echo "[bootstrap $(date +%H:%M:%S)] $*"; }

# 代码目录：脚本文件本身就在仓库里，向上取两级即是仓库根
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
cd "$APP_DIR"
log "代码目录: $APP_DIR"

# ---------------------------------------------------------------------------
# 1. 内核参数：fs.aio-max-nr（不设置的话第 25 个实例起容器必崩，见部署说明第八节坑 1）
# ---------------------------------------------------------------------------
NEED_AIO=$(cat /proc/sys/fs/aio-max-nr)
if [ "$NEED_AIO" -lt 1048576 ]; then
    sysctl -w fs.aio-max-nr=1048576
    echo "fs.aio-max-nr = 1048576" > /etc/sysctl.d/99-mysql-lab.conf
    log "已调高 AIO 上限: $NEED_AIO -> 1048576"
else
    log "AIO 上限已就绪 ($NEED_AIO)"
fi

# ---------------------------------------------------------------------------
# 2. 2G swap + 低 swappiness（无 swap 时高密度实例打满内存，Docker daemon 会超时）
# ---------------------------------------------------------------------------
if ! swapon --show=NAME | grep -q .; then
    log "创建 2G swap..."
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
    sysctl -w vm.swappiness=10
    echo "vm.swappiness = 10" > /etc/sysctl.d/99-mysql-lab-swap.conf
else
    log "swap 已存在: $(swapon --show=NAME --noheadings | head -1)"
fi

# ---------------------------------------------------------------------------
# 3. Docker 引擎（Anolis 用 dnf，Ubuntu 用 apt；已装则跳过）
# ---------------------------------------------------------------------------
if ! command -v docker > /dev/null 2>&1; then
    log "安装 Docker..."
    if command -v dnf > /dev/null 2>&1; then
        dnf install -y docker-ce docker-ce-cli containerd.io \
            || dnf install -y docker    # 阿里云 Anolis 预置 docker-ce 源；否则退系统源（需核实源名）
        systemctl enable --now docker
    else
        apt-get update -y && apt-get install -y docker.io
        systemctl enable --now docker
    fi
else
    log "Docker 已安装: $(docker --version)"
fi

# 预拉学生实例镜像（不预拉的话，开课时全班等它下载）
if ! docker image inspect mysql:8.0 > /dev/null 2>&1; then
    log "拉取 mysql:8.0（国内直连可能需要镜像加速，见部署说明第六节第 2 步）..."
    docker pull mysql:8.0
else
    log "mysql:8.0 镜像已就绪"
fi

# ---------------------------------------------------------------------------
# 4. 代码：不在仓库目录时从 git 拉取（打镜像流程里代码已随机器带过来，会跳过）
# ---------------------------------------------------------------------------
if [ ! -f "$APP_DIR/requirements.txt" ]; then
    log "当前目录没有代码，从 git 拉取..."
    git clone "${REPO_URL:?请用 REPO_URL=<仓库地址> 指定仓库}" /root/mysql-lab
    cd /root/mysql-lab
fi

# ---------------------------------------------------------------------------
# 5. Python venv + 依赖（版本上界见 requirements.txt 注释；不装系统 site-packages，
#    这是部署说明第八节坑 4 的教训：cron 用系统解释器会 import 失败）
# ---------------------------------------------------------------------------
if [ ! -x venv/bin/python ]; then
    log "创建 venv 并安装依赖..."
    python3 -m venv venv
fi
venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
log "依赖安装完成: $(venv/bin/pip list 2>/dev/null | wc -l) 个包"

# ---------------------------------------------------------------------------
# 6. systemd 单元（开机自启 + 崩溃自愈，两个发行版通吃）
# ---------------------------------------------------------------------------
install -m 644 deploy/mysql-lab.service /etc/systemd/system/mysql-lab.service
# 公网 IP 自动探测：LAB_PUBLIC_HOST=auto 时应用启动时自己从云 metadata 拿，
# 这样做出来的镜像在任何新机器上都零手工配置（这是支持"用完即释放"的关键）
mkdir -p /etc/systemd/system/mysql-lab.service.d
cat > /etc/systemd/system/mysql-lab.service.d/env.conf << 'UNIT'
[Service]
Environment=LAB_PUBLIC_HOST=auto
Environment=PYTHONUNBUFFERED=1
UNIT
systemctl daemon-reload
systemctl enable mysql-lab
systemctl restart mysql-lab

# ---------------------------------------------------------------------------
# 7. 等服务就绪并自检（相当于接手自检清单的第一条）
# ---------------------------------------------------------------------------
log "等待服务就绪..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/healthz > /dev/null 2>&1; then
        log "服务就绪: $(curl -s http://127.0.0.1:8000/healthz)"
        break
    fi
    [ "$i" = 30 ] && { log "错误：服务 30 次探测未就绪，查 journalctl -u mysql-lab"; exit 1; }
    sleep 2
done

# ---------------------------------------------------------------------------
# 8. 金镜像（可选，WITH_GOLD=1）：建实例 85s -> ~15s 的关键
#    打自定义镜像前务必带上，否则每台新机器第一节课都是慢路径
# ---------------------------------------------------------------------------
if [ "${WITH_GOLD:-0}" = "1" ]; then
    if venv/bin/python -c "from app import config; import sys; sys.exit(0 if config.GOLD_DIR.exists() and any(config.GOLD_DIR.iterdir()) else 1)" 2>/dev/null; then
        log "金镜像已存在，跳过"
    else
        log "生成 MySQL 金镜像（一次性，约 40s）..."
        venv/bin/python scripts/make_gold.py
    fi
fi

# ---------------------------------------------------------------------------
# 9. 冒烟验收：接口 + 生命周期 + 安全（不需要 Docker 实例的那部分）
# ---------------------------------------------------------------------------
log "冒烟验收..."
venv/bin/python scripts/task5_test.py > /tmp/bootstrap-task5.log 2>&1 \
    && log "生命周期 22 项: 通过" || { log "警告：task5 验收未全过，看 /tmp/bootstrap-task5.log"; }
venv/bin/python scripts/security_test.py > /tmp/bootstrap-sec.log 2>&1 \
    && log "安全基线 30 项: 通过" || { log "警告：安全验收未全过，看 /tmp/bootstrap-sec.log"; }

PUBLIC_IP=$(curl -sf --max-time 3 http://100.100.100.200/latest/meta-data/eipv4 \
    || curl -sf --max-time 3 http://100.100.100.200/latest/meta-data/public-ipv4 || echo "未知")
log "=============================================="
log "完成。教师端: http://${PUBLIC_IP}:8000"
log "做自定义镜像前最后两步（控制台操作）："
log "  1. systemctl stop mysql-lab   # 镜像里不留运行态"
log "  2. 控制台 -> 实例 -> 创建自定义镜像"
log "=============================================="
