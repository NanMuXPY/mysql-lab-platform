# MySQL 实验平台（mysql-lab-platform）

高校数据库课的实验环境交付平台：教师点一下，全班每个学生拿到一台**独立的 MySQL 8.0 实例**（独立端口、独立数据、独立账号），下课自动回收。

一台 4 核 8G 云服务器稳定支撑 **30 人同时实验**（实测 72.7 秒全部就绪、30/30 登录成功），成本只有"按人买云数据库"的零头。

## 实测数字

| 指标 | 数值 | 说明 |
|---|---|---|
| 30 人全班交付 | **72.7s** | 4核8G，金镜像路径，30/30 成功 |
| 20 人交付 | 49.3s | 2核4G 上是 60.6s，4 核快 18% |
| 单实例内存 | **139 MiB** | 限额 256 MiB，只用 54% |
| 容量上限 | 44 人 | 再多人系统就失去响应，不建议 |
| 建实例耗时 | 85s → **~15s** | 金镜像预初始化，跳过 `mysqld --initialize` |

> 完整数据与定位过程：`docs/8G容量实测-20261031.md`、`docs/压测报告-*.md`

## 核心特性

- **⚡ 上课模式**：选课程 + 起始学号 + 人数 → 一个按钮开课，实验名自动带日期
- **批量交付**：并发创建 N 个独立容器（端口池 33001-33100，CPU/内存限额），进度实时轮询，凭据导出 CSV（UTF-8 BOM，Excel 直开）
- **学习闭环三件套**
  - 教师端「错题诊断」：增量采集每台实例的 `general_log`，用 sqlglot AST 判定语法错误 / 表不存在 / 列不存在 / 无 WHERE 的危险 UPDATE / JOIN 缺 ON，并**按学生还原他实际敲过的每一条命令**
  - 教师端「练习报告 CSV」：一人一行，正确率 + 五类错误计数 + 最近一条问题命令；**没练过的学生也占一行**，谁没开始一目了然
  - 学生端 `/my`：学号 + 数据库密码登录，看自己的成功率、错因**改法建议**、自己的命令回放（不建学生账号体系）
- **资源治理**：端口租约/归还、僵尸实例对账（`reconcile.py`）、到期自动回收（systemd timer + cron）
- **安全基线**：JWT 角色门禁、PBKDF2 密码哈希、Fernet 加密存储学生库密码、容器 cap-drop、学生接口只认 token 内的学号（改 URL 也看不到别人的数据）

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / Uvicorn |
| 元数据 | SQLite（WAL + `BEGIN IMMEDIATE` + 进程内写锁） |
| 容器 | Docker SDK for Python（mysql:8.0，0.5 核 / 256 MB 限额） |
| 认证 | JWT (HS256) + PBKDF2 + Fernet |
| SQL 分析 | sqlglot（MySQL 方言 AST） |
| 前端 | 原生 HTML/JS，无构建链 |

## 快速开始

```bash
# 1. 依赖
python3 -m venv venv && venv/bin/pip install -r requirements.txt

# 2. 关键内核参数（不设置的话，第 25 个实例起容器会反复重启，且报错完全不指向这里）
sysctl -w fs.aio-max-nr=1048576
echo "fs.aio-max-nr = 1048576" > /etc/sysctl.d/99-mysql-lab.conf

# 3. 首次启动（自动建表、建种子账号、生成 .env 密钥）
venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4.（可选）生成金镜像，建实例从 85s 降到 15s
venv/bin/python scripts/make_gold.py
```

打开 `http://<服务器IP>:8000` 进教师端，学生端是 `/my`。

> `.env` 里的 `APP_SECRET` 是学生数据库密码的解密密钥，**务必和 `data/app.db` 一起备份**，丢了密码就解不开了。

## 测试

```bash
python scripts/task9_test.py http://<host>:8000          # 接口验收（含 6 条鉴权边界）
python scripts/task5_test.py                             # 生命周期 22 项
python scripts/ui_e2e_task9.py http://<host>:8000        # 真浏览器 E2E：开课→交付→导出→学生端
python scripts/stress_test.py --n 30                     # 真实 API 压测并出报告
```

## 目录结构

```
app/            后端（main.py 入口；labs 编排、insights 采集诊断、docker_svc 容器层）
app/static/     index.html 教师端 / my.html 学生端
deploy/         systemd unit
scripts/        验收脚本、压测、金镜像、对账
templates_sql/  课程 SQL 模板
docs/           设计方案、压测报告、容量实测、部署说明
```

## 已知限制

- MySQL 的 1064 纯语法错误在解析阶段就被拒绝，**不写入 `general_log`**，因此客户端侧的语法错采集不到（平台通过 sqlglot 预解析同一条语句来判定，已在服务端侧覆盖）
- 单机方案的上限就是单机：超过 40 人建议 8核16G 或按班级拆两台
- 未做 HTTPS，生产部署建议前置 Nginx + 证书
