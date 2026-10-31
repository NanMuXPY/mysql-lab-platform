"""main.py — FastAPI 入口、路由挂载。

MVP 接口进度（交接文档第六节 8 个接口）：
  [x] POST /api/auth/login        任务 3
  [x] GET  /api/templates         任务 3（同时充当角色门禁 403 验收点）
  [x] GET  /api/me                任务 3（调试用）
  [x] POST /api/labs              任务 4（批量创建，后台线程池限流）
  [x] GET  /api/labs/{id}         任务 4（进度轮询）
  [x] POST /api/labs/{id}/stop    任务 5（停止，容器与数据保留）
  [x] POST /api/labs/{id}/recycle 任务 5（回收，不可逆，删数据还端口）
  [x] POST /api/instances/{id}/reset  任务 5（重置，数据复原密码更换）
  [x] GET  /api/labs/{id}/export  任务 6（CSV，utf-8-sig）
  [x] GET  /api/instances/overview 任务 6（学生信息总览）
  [x] GET  /api/audit            任务 6（审计日志页面）

启动: python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import csv
import io
import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import audit, config, crypto, insights, labs, models, ports
from .auth import create_student_token, get_student_ctx
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
def api_lab_errors(lab_id: int, refresh: bool = False,
                   user: dict = Depends(require_role("teacher", "assistant"))):
    """错题诊断（P1）：增量采集学生 SQL，返回每人练习量/问题分布/命令时间线。

    refresh=true 跳过 30s 缓存：教师刚让学生敲完 SQL 就点诊断时，需要看到最新内容。
    """
    return insights.lab_errors(lab_id, refresh=refresh)


@app.get("/api/labs/{lab_id}/report.csv")
def api_lab_report(lab_id: int,
                   user: dict = Depends(require_role("teacher", "assistant"))):
    """练习报告 CSV：一名学生一行，供期末平时分与讲评用（utf-8-sig 保证 Excel 不乱码）。

    与「导出连接信息 CSV」分工：那个是分发票据（含密码），这个是看学得怎么样。
    """
    rows = insights.report_rows(lab_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["学号", "实例状态", "练习总数", "无问题", "有问题", "语法错误", "表不存在",
                "列不存在", "危险操作", "写法提示", "正确率(%)", "最后活跃",
                "最近一条问题命令", "诊断"])
    for r in rows:
        w.writerow([r["student_id"], r["instance_status"], r["total"], r["ok"],
                    r["problems"], r["syntax_error"], r["unknown_table"],
                    r["unknown_column"], r["dangerous"], r["hint"], r["accuracy"],
                    r["last_active"], r["last_problem_sql"], r["last_problem_detail"]])
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=lab_{lab_id}_report.csv"},
    )


# ---------------------------------------------------------------------------
# 任务 6：学生信息总览 + 审计日志
# ---------------------------------------------------------------------------
@app.get("/api/instances/overview")
def api_instances_overview(user: dict = Depends(require_role("teacher", "assistant"))):
    """学生信息总览：未回收实验单的全部实例（不含密码，凭据走 CSV）。"""
    return labs.overview()


@app.get("/api/audit")
def api_audit(limit: int = 100,
              user: dict = Depends(require_role("teacher", "assistant"))):
    """最近审计日志（登录/创建/停止/回收/重置全留痕），最多 500 条。"""
    return audit.recent(min(max(limit, 1), 500))


# ---------------------------------------------------------------------------
# 任务 5：实例生命周期（停止 / 回收 / 重置）
# ---------------------------------------------------------------------------
@app.post("/api/labs/{lab_id}/stop")
def api_stop_lab(lab_id: int, request: Request,
                 user: dict = Depends(require_role("teacher", "assistant"))):
    """停止实验单全部实例（容器保留，学生暂时连不上；数据与端口不动）。"""
    result = labs.stop_lab(lab_id)
    audit.log(user["id"], "lab.stop", target=str(lab_id),
              detail=f"stopped={result['stopped']}",
              ip=request.client.host if request.client else "")
    return result


@app.post("/api/labs/{lab_id}/recycle")
def api_recycle_lab(lab_id: int, request: Request,
                    user: dict = Depends(require_role("teacher", "assistant"))):
    """回收实验单（不可逆）：删容器与数据卷，端口归还池子。"""
    result = labs.recycle_lab(lab_id)
    audit.log(user["id"], "lab.recycle", target=str(lab_id),
              detail=f"recycled={result['recycled']}",
              ip=request.client.host if request.client else "")
    return result


@app.post("/api/instances/{instance_id}/reset")
def api_reset_instance(instance_id: int, request: Request,
                       user: dict = Depends(require_role("teacher", "assistant"))):
    """重置单个学生实例（数据复原、密码更换；新密码仅在本次响应中返回）。"""
    result = labs.reset_instance(instance_id)
    audit.log(user["id"], "instance.reset",
              target=str(instance_id),
              detail=f"student={result['student_id']} port={result['port']}",
              ip=request.client.host if request.client else "")
    return result


# ---------------------------------------------------------------------------
# 学生端：我的练习（学习闭环 —— 学生也能看到自己敲过什么、错在哪）
# 不建学生账号：用「学号 + 数据库密码」登录，密码本来就是平台发到他手上的
# ---------------------------------------------------------------------------
class StudentLoginIn(BaseModel):
    student_id: str
    password: str


@app.post("/api/student/login")
def api_student_login(body: StudentLoginIn, request: Request):
    sid = body.student_id.strip()
    inst = models.find_active_instance_by_student(sid)
    # 统一文案：不区分"学号不存在"和"密码不对"，避免被拿来探测同学学号
    if not inst:
        raise HTTPException(401, detail="学号或密码不正确（也可能是实验已回收，请找老师确认）")
    try:
        real = crypto.decrypt(inst["db_pass_enc"])
    except Exception:  # 密钥变更 / 老数据
        raise HTTPException(400, detail="该实例凭据无法解密，请联系老师重置")
    if not secrets.compare_digest(real, body.password.strip()):
        raise HTTPException(401, detail="学号或密码不正确（也可能是实验已回收，请找老师确认）")
    audit.log(None, "student.login", target=sid,
              detail=f"lab={inst['lab_id']}",
              ip=request.client.host if request.client else "")
    return {
        "token": create_student_token(sid, inst["lab_id"], inst["id"]),
        "student_id": sid,
        "lab_id": inst["lab_id"],
        "lab_name": inst["lab_name"],
        "class_name": inst.get("class_name") or "",
    }


@app.get("/api/student/my")
def api_student_my(request: Request, ctx: dict = Depends(get_student_ctx)):
    """我的练习：连接信息 + 练习统计 + 命令回放 + 错因改法。

    只按 token 里的 sid 查数据，**不接受**任何请求参数指定学号（越权面为零）。
    """
    inst = models.get_instance(ctx["instance_id"])
    if inst["status"] == "recycled":
        raise HTTPException(404, detail="本次实验环境已回收，记录不再保留")
    lab = models.get_lab(inst["lab_id"])
    detail = insights.student_detail(inst["lab_id"], inst["student_id"],
                                     inst["container_name"])
    return {
        "student_id": inst["student_id"],
        "lab_name": lab["name"],
        "class_name": lab["class_name"],
        "conn": {
            # 没配 LAB_PUBLIC_HOST 时用请求的 host 兜底：学生从外网访问，
            # 拿到的就是他刚才访问的那个地址，比"见通知"有用
            "host": config.PUBLIC_HOST or request.url.hostname or "",
            "port": inst["port"],
            "database": inst["db_name"],
            "user": inst["db_user"],
        },
        "status": inst["status"],
        **detail,
    }


# ---------------------------------------------------------------------------
# 教师端 WebUI（唯一页面，学生不提供任何界面——学生用 Navicat/DBeaver 直连）
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/my", include_in_schema=False)
def student_page():
    return FileResponse(Path(__file__).parent / "static" / "my.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
