"""main.py — FastAPI 入口、路由挂载。

MVP 接口进度（交接文档第六节 8 个接口）：
  [x] POST /api/auth/login        任务 3
  [x] GET  /api/templates         任务 3（同时充当角色门禁 403 验收点）
  [x] GET  /api/me                任务 3（调试用）
  [x] POST /api/labs              任务 4（批量创建，后台线程池限流）
  [x] GET  /api/labs/{id}         任务 4（进度轮询）
  [ ] POST /api/labs/{id}/stop    任务 5
  [ ] DELETE /api/labs/{id}       任务 5
  [ ] POST /api/instances/{id}/reset  任务 5
  [ ] GET  /api/labs/{id}/export  任务 6

启动: python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import audit, config, insights, labs, models, ports
from .auth import login as do_login
from .auth import require_role
from .errors import NotFound, PlatformError, PortPoolExhausted

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("mysql-lab")

app = FastAPI(title="MySQL 实验环境一键交付平台", version="0.4.0")


@app.exception_handler(PlatformError)
def platform_error_handler(request: Request, exc: PlatformError):
    """业务异常 → HTTP 的统一映射（FastAPI 层只做这一件事）。"""
    if isinstance(exc, NotFound):
        code = 404
    elif isinstance(exc, PortPoolExhausted):
        code = 409  # 冲突：资源不足，前端提示回收或缩减规模
    else:
        code = 400
    return JSONResponse(status_code=code, content={"detail": str(exc)})


@app.on_event("startup")
def startup() -> None:
    config.ensure_dirs()
    models.init()
    ports.init()
    audit.init()
    insights.init()
    for w in config.check_startup():
        log.warning("启动自检: %s", w)
    seeded = models.seed_demo_users()
    if seeded:
        for login_name, pwd in seeded.items():
            log.warning("首次初始化种子账号（仅显示这一次，正式部署应删掉本种子逻辑）: "
                        "%s / %s", login_name, pwd)
    if models.seed_demo_template():
        log.info("已注入样例实验模板 sample_basic")
    for name in models.seed_presets():
        log.info("已注册预设课程模板: %s", name)
    log.info("启动完成，元数据库: %s", config.DB_PATH)


class LoginIn(BaseModel):
    login: str
    password: str


@app.post("/api/auth/login")
def api_login(body: LoginIn, request: Request):
    """登录，成功发 JWT（交接文档第六节契约: {token, role, name}）。"""
    result = do_login(body.login, body.password)
    user = models.get_user_by_login(body.login)
    audit.log(user["id"], "login", target=body.login,
              ip=request.client.host if request.client else "")
    return result


@app.get("/api/me")
def api_me(user: dict = Depends(require_role("teacher", "assistant", "student"))):
    """任何已登录角色可查自己的身份（调试用）。"""
    return user


@app.get("/api/templates")
def api_templates(user: dict = Depends(require_role("teacher", "assistant"))):
    """模板列表。教师/助教可见 —— 学生角色访问此接口即 403（任务 3 验收点）。"""
    return models.list_templates()


@app.get("/api/portpool/stats")
def api_portpool_stats(user: dict = Depends(require_role("teacher", "assistant"))):
    """端口池状态（看板用，顺手暴露给验收）。"""
    return ports.stats()


# ---------------------------------------------------------------------------
# 任务 4：实验批量创建与进度
# ---------------------------------------------------------------------------
class LabCreateIn(BaseModel):
    template_id: int
    name: str
    class_name: str = ""
    duration_min: int = 120
    student_ids: list[str]


@app.post("/api/labs", status_code=202)
def api_create_lab(body: LabCreateIn, request: Request,
                   user: dict = Depends(require_role("teacher", "assistant"))):
    """创建实验单并批量交付（202 已受理，前端用 GET /api/labs/{id} 轮询进度）。"""
    lab_id = labs.create_lab_batch(
        teacher_id=user["id"], template_id=body.template_id, name=body.name,
        class_name=body.class_name, duration_min=body.duration_min,
        student_ids=body.student_ids,
    )
    audit.log(user["id"], "lab.create", target=str(lab_id),
              detail=f"name={body.name} students={len(body.student_ids)}",
              ip=request.client.host if request.client else "")
    return {"lab_id": lab_id, "total": len(body.student_ids)}


@app.get("/api/labs")
def api_list_labs(user: dict = Depends(require_role("teacher", "assistant"))):
    """实验单列表（教师端首页加载用）。

    注意：必须显式声明 GET，否则前端 GET /api/labs 会撞到 POST 路由返回 405，
    整块实验单区域渲染为空——这个问题真浏览器测试才抓得到。
    """
    return labs.list_labs()


@app.get("/api/labs/{lab_id}")
def api_lab_progress(lab_id: int,
                     user: dict = Depends(require_role("teacher", "assistant"))):
    """实验单进度：summary 汇总 + 每个学生的实例状态（不含密码）。"""
    return labs.lab_progress(lab_id)


@app.get("/api/labs/{lab_id}/export")
def api_export_lab(lab_id: int, request: Request,
                   user: dict = Depends(require_role("teacher", "assistant"))):
    """导出学生连接信息 CSV（含密码，教师分发票据用）。"""
    rows = labs.export_credentials(lab_id)
    host = config.PUBLIC_HOST or request.url.hostname
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["student_id", "host", "port", "database", "user", "password", "status"])
    for r in rows:
        w.writerow([r["student_id"], host, r["port"], r["db_name"],
                    r["db_user"], r["db_password"], r["status"]])
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),  # BOM：Excel 直接打开不乱码
        media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=lab_{lab_id}_credentials.csv"},
    )


@app.get("/api/labs/{lab_id}/errors")
def api_lab_errors(lab_id: int,
                   user: dict = Depends(require_role("teacher", "assistant"))):
    """错题诊断（P1）：增量采集学生 SQL 后，返回每人练习量/问题分布/最近错例。"""
    return insights.lab_errors(lab_id)


# ---------------------------------------------------------------------------
# 教师端 WebUI（唯一页面，学生不提供任何界面——学生用 Navicat/DBeaver 直连）
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
