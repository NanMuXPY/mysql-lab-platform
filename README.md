# MySQL 实验平台（mysql-lab-platform）

基于 **Docker + FastAPI** 的高校 MySQL 课程实验环境一键交付平台。教师端 WebUI 一键为全班学生批量生成独立 MySQL 实例，单机即可支撑 20+ 学生并发实验，成本远低于按人购买云数据库。

## 核心特性

- **一键批量交付**：教师导入学号名单 → 并发创建 N 个独立 MySQL 8.0 容器（端口池 33001-33100，CPU/内存限额）→ 实时查看创建进度 → 凭据一键导出 CSV（utf-8-sig，Excel 直开）
- **教师端 WebUI**：单页应用，登录 / 选课卡片 / 快捷生成学号 / 批量部署 / 进度轮询 / 错题诊断
- **学习闭环（错题诊断）**：增量采集学生实例 general_log，基于 sqlglot AST 分类错误（表不存在 / 危险操作 / 语法写法提示），教师可查看每个学生的典型失误
- **资源治理**：端口租约/归还/对账（reconcile），僵尸实例自动回收；部分唯一索引保证活跃实例端口不冲突
- **安全基线**：JWT 角色门禁（teacher/assistant/student）、PBKDF2 密码哈希、Fernet 加密存储学生数据库密码、容器 cap-drop、时区 Asia/Shanghai
- **课程模板**：内置 4 门预设课程 SQL 模板（单表查询 / 图书借阅 / 电商订单 / 多表连接）+ 基础示例

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11 + FastAPI + Uvicorn |
| 元数据 | SQLite（WAL + BEGIN IMMEDIATE + 进程内写锁） |
| 容器 | Docker SDK for Python（mysql:8.0） |
| 认证 | JWT (HS256) + PBKDF2 + Fernet |
| SQL 分析 | sqlglot（AST 判错） |
| 前端 | 原生 HTML/JS 单页 |

## 目录结构

```
app/               后端主应用（main.py 入口；models/db/ports/labs/insights 等模块）
app/static/        教师端单页 WebUI
templates_sql/     课程 SQL 模板（预设课程 + 示例）
scripts/           部署/测试脚本（task*_test.py 分阶段验收、ui_e2e.py 浏览器端到端、reconcile.py 对账）
docs/              设计方案、交接文档、进展报告
```

## 快速开始

```bash
# 依赖
pip install fastapi uvicorn docker cryptography sqlglot python-multipart

# 启动（需本机 Docker 环境）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动自动建表并创建种子账号（密码可通过环境变量 `TEACHER_INIT_PASSWORD` 等覆盖，登录后请立即修改）。

## 测试

```bash
python scripts/task2_test.py   # 端口池与认证 11 项
python scripts/task3_test.py   # 批量创建 13 项
python scripts/task4_test.py   # WebUI/CSV/诊断 19 项
python scripts/ui_e2e.py http://<host>:8000   # 真浏览器 E2E 17 项
```

## 路线图

- [ ] 实例重置 / 停止 / 回收生命周期管理
- [ ] SQL 代理层实时拦截（解决 MySQL general_log 不记录 1064 的已知限制）
- [ ] 4核8G 规格全班并发压测
- [ ] HTTPS / 安全加固
