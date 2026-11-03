# 交接文档 · MySQL 实验环境一键交付平台（开发启动版）

> ⚠️ **本文已过时，仅作存档。** 这是项目动手前写的"计划版"，其中 docker-compose、
> restart=on-failure、部分任务拆分均与最终实现不一致（实测后改成了 systemd 与 unless-stopped）。
> 接手请读 **`docs/交付说明书-v1.0.2.md`**。
>
> 交接日期：2026-10-30
> 建议新工作空间根目录：`mysql-lab`（本文件已放在这里）
> 用户身份：深圳职业技术大学 云计算技术应用专业学生（2027 届），本人独立开发，项目简介项目之一
> 目标岗位映射：通用云计算岗 / 云运维 / FDE（前沿部署工程师）

---

## 一、一句话与成功标准

**一句话**：把"给学生配 MySQL 实验环境"这件手工活变成一个按钮——教师选模板、填人数和时长，一次操作完成全班环境下发，学生用本地工具（Navicat / DataGrip / mysql 命令行）直连自己的独立实例，下课到期自动回收。

**MVP 验收标准（做完这五条就算成）**：
1. 教师登录 → 建模板 → 开课（20 人），**90 秒内**全班实例状态变为 `running`
2. 学生拿到连接信息表，用 Navicat 连 `服务器IP:330xx` 成功，能看到自己的库和初始数据
3. 学生改坏数据 → 教师点"重置"，**10 秒内**恢复初始状态
4. 教师点"回收" → 容器与数据卷全部删除，端口回到池子（`docker ps -a` 无残留）
5. 同时开 20 个实例，主机内存占用可测，无 OOM、无端口冲突

---

## 二、环境与依赖

| 项 | 要求 | 备注 |
|---|---|---|
| 开发机 | Windows + Docker Desktop（用户已有）；正式部署在阿里云 ECS（4C8G 建议） | 本地先跑通再上云 |
| Python | 3.10+ ，`pip install fastapi uvicorn[standard] docker jinja2 python-multipart itsdangerous passlib[bcrypt]` | `docker` 是官方 Docker SDK |
| Docker | 需要能访问 Docker socket；Windows 下走 `npipe:////./pipe/docker_engine` | 报权限问题时优先查 Docker Desktop 是否运行 |
| MySQL 镜像 | `mysql:8.0`（提前 `docker pull`，避免建实例时慢） | 一次性拉取 |
| 端口段 | 33001–33100（租给实例），平台自身 8000/443 | 阿里云安全组需放通该段并限源 IP |

**开发顺序**：先在本地 Docker Desktop 用 **2–3 个实例**验证全流程 → 再上 ECS 压 20 个。

---

## 三、技术栈与目录结构

- 后端：FastAPI + Docker SDK（Python）+ SQLite（元数据库，后期可换 MySQL）
- 前端：Jinja2 模板 + 原生 HTML/JS（**不要引入前端框架**，教师端只有两个页面：开课、实例列表）
- 部署：`docker-compose`（nginx + api 两容器），`restart: unless-stopped`

```
mysql-lab/
├─ app/
│  ├─ main.py            # FastAPI 入口、路由挂载
│  ├─ auth.py            # 登录、JWT 签发与校验、角色判断
│  ├─ docker_svc.py      # 容器创建/销毁/重置（唯一碰 Docker SDK 的地方）
│  ├─ ports.py           # 端口池：租约、回收、对账
│  ├─ labs.py            # 实验单与实例的增删停
│  ├─ models.py          # SQLite 表结构（SQLAlchemy 或原生 sqlite3）
│  ├─ audit.py           # 审计日志写入（装饰器或中间件）
│  └─ templates/         # 教师开课页、实例列表页、学生信息页
├─ data/
│  ├─ app.db             # 元数据库
│  └─ labs/<lab_id>/<student_id>/   # 每实例的数据卷目录（挂到 /var/lib/mysql）
├─ templates_sql/        # 实验模板 SQL（建表 + 初始数据 + 题目说明）
├─ scripts/
│  ├─ smoke_test.py      # 建 3 个实例并验证连接
│  └─ stress_test.py     # 建 20 个实例，采样内存占用与耗时，出实测数据
├─ docker-compose.yml
└─ docs/交接-MySQL实验平台.md（本文件）
```

---

## 四、数据模型（6 张表）

```sql
users(id, login, name, role, password_hash, created_at)
  -- role: teacher | assistant | student

templates(id, name, description, init_sql_path, version, created_at)

labs(id, template_id, teacher_id, name, class_name,
     duration_min, allowed_cidr, status, starts_at, ends_at)
  -- status: pending | creating | running | stopped | recycled | failed

instances(id, lab_id, student_id, container_name, volume_path, port,
          db_name, db_user, db_pass_enc, status, created_at, last_error)
  -- status: creating | running | stopped | recycled | failed（与容器状态机一致）

port_pool(port, instance_id, leased_at)      -- 端口租约，防止复用冲突
audit_logs(id, actor_id, action, target, detail, ip, created_at)
```

**约定**：
- 学生密码用对称加密存（密钥走环境变量 `APP_SECRET`），**不存明文**；教师端只在创建时展示一次
- `instances.port` 唯一索引，与 `port_pool` 一对一
- 任何创建失败必须写 `last_error` 并归还端口（这是最容易漏的一步）

---

## 五、容器创建参数（照抄，别自己改）

```bash
docker run -d \
  --name lab-<lab_id>-<student_id> \
  --cpus=0.5 --memory=256m --memory-swap=256m --pids-limit=200 \
  --cap-drop ALL --security-opt no-new-privileges \
  -e MYSQL_ROOT_PASSWORD=<随机16位> \
  -e MYSQL_DATABASE=<db_name> \
  -e MYSQL_USER=<stu_学号> \
  -e MYSQL_PASSWORD=<随机16位> \
  -v <volume_path>:/var/lib/mysql \
  -v <init_sql_abs>:/docker-entrypoint-initdb.d/01-init.sql:ro \
  -p <host_port>:3306 \
  --restart=on-failure:3 \
  --log-opt max-size=10m --log-opt max-file=2 \
  mysql:8.0 \
  --innodb-buffer-pool-size=64M \
  --performance-schema=OFF \
  --max-connect-errors=10 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
```

**已知坑（都验证过会踩）**：
- `--cap-drop ALL` 后镜像初始化脚本可能失败（需要 `chown` 权限）→ 若报错，先加回 `--cap-add CHOWN --cap-add SETUID --cap-add SETGID`，再逐项移除找出最小集合
- `--memory=256m` 必须同时给 `--memory-swap=256m`，否则容器可用 swap 反而更慢更不稳
- 初始化 SQL 只在**数据卷为空**时执行一次；"重置"必须删容器 + **清空数据卷目录**再重建，否则初始数据不会重跑
- 容器日志不限制会写满磁盘（教学平台最常见事故），`--log-opt` 两条不能省

---

## 六、API 契约（MVP 共 8 个）

| 方法 | 路径 | 请求要点 | 响应要点 |
|---|---|---|---|
| POST | `/api/auth/login` | `{login, password}` | `{token, role, name}` |
| GET | `/api/templates` | — | `[{id, name, description}]` |
| POST | `/api/labs` | `{template_id, name, class_name, students:[{student_id,name}], duration_min, allowed_cidr}` | `{lab_id, task_id, status:"creating"}` |
| GET | `/api/labs/{id}` | — | `{lab:{...}, instances:[{student_id, port, db_name, db_user, status, last_error}]}` |
| POST | `/api/labs/{id}/stop` | — | `{affected:n}`（保留数据卷） |
| DELETE | `/api/labs/{id}` | — | `{recycled:n}`（删容器 + 删卷 + 还端口） |
| POST | `/api/instances/{id}/reset` | — | `{status:"creating"}` → 轮询到 `running` |
| GET | `/api/labs/{id}/export` | — | CSV：`学号,姓名,主机,端口,库名,账号,密码` |

**异步约定**：创建/重置接口**立即返回**，前端每 2 秒轮询实验单详情。后台用线程池控制并发 **最多 5 个容器同时创建**（30 个串行约 60 秒，全并发会打满磁盘 IO）。

**响应示例**：
```json
POST /api/labs → 202
{"lab_id": 7, "task_id": "8f2c", "status": "creating", "total": 20}

GET /api/labs/7 → 200
{"lab": {"id":7,"status":"running","ends_at":"2026-11-02T10:00:00"},
 "instances":[{"student_id":"240101","port":33017,"db_name":"lab_240101",
               "db_user":"stu_240101","status":"running","last_error":null}]}
```

---

## 七、安全约束（学生直连公网，这几条不能省）

1. **端口段收敛** 33001–33100，安全组只放通该段 + `allowed_cidr`（校园网段）；离职/结课立即收回
2. **一人一账号，库级授权**，`root` 密码只有平台知道，从不发给学生
3. **强密码** 16 位随机；`max-connect-errors=10` 防爆破
4. **容器收敛**：`--cap-drop ALL`、非 root、`--pids-limit`、只挂自己的卷
5. **不要把 Docker socket 挂进任何容器**（等于把宿主机控制权交出去）
6. **必须主动承认**：容器共用内核，**不是安全边界**；所以代码里把"创建实例"抽象成 `provider` 接口，将来换 KVM/轻量云主机不动上层

---

## 八、任务拆解（按半天，含验收）

| # | 任务 | 验收标准 |
|---|---|---|
| 1 | `docker_svc.py`：用 SDK 建/删/查单个 MySQL 容器 | 命令行建出的容器，Navicat 能连上并看到初始表 |
| 2 | `ports.py`：端口池租约与归还 | 并发租 5 个端口不重复；归还后可复用 |
| 3 | `models.py` + 登录鉴权 | 教师能登录拿到 JWT，学生角色访问教师接口被拒 403 |
| 4 | `POST /api/labs` 批量创建（线程池限 5） | 10 人实验单 60 秒内全部 running，失败项有 `last_error` |
| 5 | 重置 + 停止 + 回收 | 重置 10 秒内恢复初始数据；回收后 `docker ps -a` 无残留、端口全归还 |
| 6 | 教师页 + 学生信息页 + 导出 CSV | 页面能开课、能看到每台实例状态，CSV 能直接发给学生 |
| 7 | `stress_test.py` 压 20 实例 | 出报告：总耗时、内存占用、单实例均值（写进项目简介的数字来源） |
| 8 | 部署到 ECS + docker-compose + 日志 | 重启机器后服务自动起来，元数据不丢 |

**明确不做（MVP 边界）**：自动评分、题库管理、多机调度、学生自助注册、Web 版 SQL 客户端、K8s 化。这些进第二期。

---

## 九、需要用户（卞琪翔）提供的信息

1. **实验模板 SQL**：老师课上实际用的建表 + 初始数据 + 题目。没有就先给个三表十题的样例（student / course / score），能跑通即可
2. **班级名单**：学号 + 姓名，10 人就够演示
3. **一位愿意试的老师**：这一步决定项目简介能不能写"已实际使用"，优先级高于任何功能
4. **服务器信息**：阿里云 ECS 配置、地域、是否已有 Docker、安全组是否可改
5. **是否要 TLS**：`--require-secure-transport=ON` 更安全但学生端要配证书，教学场景可先不做，说明时说明取舍

---

## 十、项目简介条目（可直接用，通用云计算岗版）

> **MySQL 实验环境一键交付平台**｜独立设计与开发
> **项目背景**：实验课教师需为全班手工准备 MySQL 环境，课前准备 1–2 小时，版本不一、误删库频发、课后环境不清算。
> **职责**：独立完成架构设计、容器生命周期管理、端口与权限隔离方案、批量下发与回收、审计与监控设计。
> **项目成果**：教师一次操作 1 分钟内完成 30 人环境交付，课前准备时间从 2 小时降至分钟级；一人一实例的端口、账号、数据卷三重隔离，全操作留审计日志；单台 4C8G 云服务器实测承载 20 实例并行，较逐一发放云主机显著降本。

**数字必须来自第 8 项的压测报告**，不要凭感觉写。

---

## 十一、完整设计方案与说明材料

`项目简介/MySQL实验平台-设计方案.md` —— 12 节，含容量成本测算、可观测性方案、8 条说明追问答法、风险清单。**开发前先通读一遍**，尤其第十一节（说明答法），它决定了代码里哪些设计必须做对。

---

## 十二、相关背景（换工作空间后了解用）

- 用户的项目简介工作在本会话另一个工作空间 `项目简介`，三份定向文案已定稿（FDE / 云运维 / 通用云），模块顺序与口径红线在 `项目简介/交接文档.md`
- 项目简介一律三段式：项目背景 / 职责 / 项目成果，**不堆技术细节**
- 用户成本敏感：不要在阿里云上长期占用实例；测试完即停；上云前先给成本估算
- **不允许编造**：功能没做完就写"设计中"，不能写成已上线
