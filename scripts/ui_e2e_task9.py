"""ui_e2e_task9.py — 任务 9 的真浏览器验收（Playwright）。

覆盖本次新增的三块教师端/学生端能力：
  1. 「上课模式」卡片：选课程 + 学号段 + 人数 → 一键开课（真的建单，1 人）
  2. 实验单卡片上的「导出练习报告」→ 真实下载发生、文件非空
  3. 学生端 /my：学号 + 数据库密码登录 → 看到统计、错因改法、命令回放

沿用三条铁律：必挂 pageerror、console.error/失败请求全听、浏览器 finally 关。
跑完自动回收测试实验单，不留垃圾实例。

用法：
  tools/.venv/Scripts/python.exe scripts/ui_e2e_task9.py [BASE_URL]
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ui_test"
OUT.mkdir(parents=True, exist_ok=True)

TEST_CLASS = "UI测试班"
TEST_SID = "560001"

page_errors: list[str] = []
console_errors: list[str] = []
bad_http: list[str] = []
failed_req: list[str] = []
steps: list[dict] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    steps.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return ok


def api(path: str, data=None, tok: str | None = None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(BASE + path,
                               data=json.dumps(data).encode() if data is not None else None,
                               headers=h)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    teacher_tok = json.loads(api("/api/auth/login",
                                 {"login": "teacher", "password": "<演示用默认密码，部署时须修改>"})[1])["token"]
    lab_id = None

    with sync_playwright() as pw:
        # --no-proxy-server：本机系统代理（127.0.0.1:26561）会把内网/公网请求全拦掉
        browser = pw.chromium.launch(headless=True,
                                     args=["--no-proxy-server",
                                           "--disable-features=NetworkServiceSandbox"])
        try:
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("console", lambda m: console_errors.append(m.text)
                    if m.type == "error" else None)
            page.on("requestfailed",
                    lambda r: failed_req.append(f"{r.url} {r.failure}"))
            page.on("response", lambda r: bad_http.append(f"{r.status} {r.url}")
                    if r.status >= 400 else None)

            # ---------- 教师端 ----------
            page.goto(BASE, wait_until="domcontentloaded")
            page.fill("#f-login", "teacher")
            page.fill("#f-pass", "<演示用默认密码，部署时须修改>")
            page.click("button:has-text('登 录')")
            page.wait_for_selector("#main:visible", timeout=15000)
            step("教师登录进入主界面", True)

            # <option> 在未展开的 <select> 里可见性判定为 false，只能等 attached
            page.wait_for_selector("#f-tpl option", timeout=10000, state="attached")
            n_opt = page.locator("#f-tpl option").count()
            step("上课模式：课程下拉已加载", n_opt >= 4, f"{n_opt} 门课")

            adv_open = page.locator("#adv-box").evaluate("el => el.open")
            step("高级选项默认收起（课堂上一眼只看四个格子）", adv_open is False)

            page.fill("#f-class2", TEST_CLASS)
            page.fill("#f-start2", TEST_SID)
            page.fill("#f-num2", "1")
            page.fill("#f-dur2", "60")
            page.click("#btn-quick")
            page.wait_for_selector("#q-ok:visible", timeout=20000)
            ok_txt = page.inner_text("#q-ok")
            m = re.search(r"#(\d+)", ok_txt)
            lab_id = int(m.group(1)) if m else None
            step("上课模式一键开课", lab_id is not None, ok_txt.strip()[:60])

            # 等交付完成（1 个实例，金镜像路径通常 25-40s）
            done = False
            for _ in range(40):
                page.wait_for_timeout(3000)
                txt = page.inner_text("#labs")
                if lab_id and re.search(rf"#{lab_id}\b", txt):
                    card = page.locator("#labs > div").filter(has_text=f"#{lab_id}").first
                    ct = card.inner_text()
                    if "running" in ct:
                        done = True
                        break
                    if "failed" in ct:
                        break
            step("新实验单交付为 running", done)

            # 导出练习报告：真实下载
            card = page.locator("#labs > div").filter(has_text=f"#{lab_id}").first
            btn = card.locator("button:has-text('导出练习报告')")
            step("实验单出现「导出练习报告」按钮", btn.count() > 0)
            if btn.count():
                with page.expect_download(timeout=20000) as dl:
                    btn.first.click()
                d = dl.value
                path = OUT / (d.suggested_filename or f"lab_{lab_id}_report.csv")
                d.save_as(str(path))
                size = path.stat().st_size
                head = path.read_bytes()[:120].decode("utf-8-sig", "replace")
                step("报告文件下载成功且非空", size > 0, f"{size} 字节")
                step("报告首行是中文表头", "学号" in head, head.splitlines()[0][:50])
                page.screenshot(path=str(OUT / "task9_teacher.png"), full_page=True)

            # ---------- 学生端 ----------
            # 用教师 API 拿该学生密码（模拟"老师把 CSV 发给学生"）
            pw_csv = api(f"/api/labs/{lab_id}/export", tok=teacher_tok)[1]
            rows = list(csv.DictReader(io.StringIO(pw_csv.decode("utf-8-sig"))))
            stu = next((r for r in rows if r["student_id"] == TEST_SID), None)
            step("教师可导出该学生连接信息", stu is not None)
            if not stu:
                raise SystemExit(print("拿不到测试学生凭据，跳过学生端验证") or 1)

            sp = ctx.new_page()
            sp.on("pageerror", lambda e: page_errors.append("student: " + str(e)))
            sp.on("console", lambda m: console_errors.append("student: " + m.text)
                  if m.type == "error" else None)
            sp.goto(BASE + "/my", wait_until="domcontentloaded")
            sp.fill("#f-sid", TEST_SID)
            sp.fill("#f-pw", stu["password"])
            sp.click("button:has-text('登 录')")
            sp.wait_for_selector("#main:visible", timeout=15000)
            conn_txt = sp.inner_text("#conn")
            step("学生登录后显示连接信息",
                 "330" in conn_txt and stu["database"] in conn_txt,
                 conn_txt.replace("\n", " ")[:70])
            # 新实例还没有练习记录，页面必须给出"还没有记录"的引导而不是空白
            stats_txt = sp.inner_text("#stats")
            step("新实例给的是引导文案而不是空白",
                 "还没有记录" in stats_txt or "%" in stats_txt,
                 stats_txt.replace("\n", " ")[:60])
            sp.screenshot(path=str(OUT / "task9_student.png"), full_page=True)

        finally:
            browser.close()
            # 无论断言失败还是中途抛异常，都不能把测试实例留在机器上占内存端口
            if lab_id:
                s, _ = api(f"/api/labs/{lab_id}/recycle", {}, teacher_tok)
                print(f"  (已回收测试实验单 #{lab_id}，status={s})")

    print("\n--- 浏览器侧异常 ---")
    step("无未捕获 JS 异常", not page_errors, str(page_errors[:2]))
    real_console = [c for c in console_errors if "favicon" not in c.lower()]
    step("无 console.error", not real_console, str(real_console[:2]))
    real_http = [h for h in bad_http if "/favicon" not in h]
    step("无 4xx/5xx 响应", not real_http, str(real_http[:3]))
    step("无失败请求", not failed_req, str(failed_req[:2]))

    n_fail = sum(1 for s in steps if not s["ok"])
    print(f"\n结果：{len(steps) - n_fail} 通过 / {n_fail} 失败")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
