"""教师端 WebUI 端到端冒烟测试（Playwright 真实浏览器）。

按 wcb-webapp-testing 三条铁律写：
  1. 必挂 pageerror —— 抓未捕获异常，静态分析永远看不到
  2. console.error / requestfailed / 4xx-5xx 全都要听 —— 抓"页面没崩但功能坏了"
  3. 浏览器必须在 finally 关 —— 否则无头 Chromium 泄漏吃内存

覆盖流程：首页 → 登录 → 课程列表 → 快捷生成学号 → 一键部署 2 人
          → 轮询到 running → 导出 CSV（真实下载）→ 错题诊断 → 造错后再诊断

用法：
  tools/.venv/Scripts/python.exe scripts/ui_e2e.py [BASE_URL]
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://<服务器公网IP>:8000"
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ui_test"
OUT.mkdir(parents=True, exist_ok=True)

# ---- 采集容器 ------------------------------------------------------------
page_errors: list[str] = []      # pageerror：未捕获异常
console_errors: list[str] = []   # console.error
bad_http: list[str] = []         # 4xx / 5xx
failed_req: list[str] = []       # 请求失败（DNS/断开/超时）
steps: list[dict] = []           # 断言结果
shots: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    steps.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))
    return ok


def shot(page, tag: str) -> None:
    p = OUT / f"{tag}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        shots.append(str(p))
    except Exception as e:  # 截图失败不该中断测试
        print(f"  (截图失败 {tag}: {e})")


def _card_text(page, lab_id: int) -> str:
    """取某实验单卡片的文本（卡片是 #labs 的直接子 div，标题里带 #id）。"""
    cards = page.locator("#labs > div")
    for i in range(cards.count()):
        t = cards.nth(i).inner_text()
        if re.search(rf"#{lab_id}(\s|$)", t):
            return t
    return ""


def main() -> int:
    lab_id = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 950},
                accept_downloads=True,
            )
            page = ctx.new_page()

            # ---- 铁律 1&2：三类监听器必须先挂，再访问页面 ----
            page.on("pageerror", lambda e: page_errors.append(f"{type(e).__name__}: {e}"))
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("requestfailed", lambda r: failed_req.append(
                f"{r.method} {r.url} :: {r.failure}"))
            page.on("response", lambda r: bad_http.append(
                f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)

            print(f"== 目标 {BASE} ==")
            # 用 domcontentloaded 而非 load/networkidle：页面登录后会每 5 秒轮询，
            # networkidle 可能永远不触发；页面本身无外部资源，DOM 就绪即可测
            for attempt in (1, 2):
                try:
                    page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print(f"  首页加载失败，重试一次：{str(e)[:90]}")
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass  # 达不到 idle 不算失败

            # 1) 首页
            h1 = page.locator("h1").first.inner_text()
            step("首页标题含『教师端』", "教师端" in h1, h1.strip())
            step("登录卡可见", page.is_visible("#login"))

            # 2) 登录（账号密码页面已预填，仍显式填一次以防预填被删）
            page.fill("#f-login", "teacher")
            page.fill("#f-pass", "<演示用默认密码，部署时须修改>")
            page.click("button:has-text('登 录')")
            try:
                page.wait_for_selector("#main", state="visible", timeout=20000)
                ok = True
                err = ""
            except Exception as e:
                ok = False
                err = str(e)[:120]
            step("登录成功进入主界面", ok, err)
            if not ok:
                shot(page, "01_login_fail")
                raise SystemExit(1)

            who = page.inner_text("#who")
            step("右上角显示登录身份", "teacher" in who, who.replace("\n", " ")[:40])

            # 关键断言：boot() 会异步 loadLabs()，若列表接口 405/异常，这块永远空白
            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('#labs > div').length > 0",
                    timeout=12000)
                n_card = page.locator("#labs > div").count()
                step("实验单列表已渲染（GET /api/labs）", True, f"{n_card} 个卡片")
            except Exception:
                txt = page.inner_text("#labs")[:80].replace("\n", " ")
                step("实验单列表已渲染（GET /api/labs）", False, f"12s 内无卡片，区域内容: {txt!r}")
                shot(page, "02b_labs_empty")

            # 3) 课程列表
            page.wait_for_selector("#tpl-grid .tpl", timeout=20000)
            n_tpl = page.locator("#tpl-grid .tpl").count()
            step(f"预设课程渲染 {n_tpl} 门", n_tpl >= 5, f"期望>=5")
            tpl_names = page.locator("#tpl-grid .tpl b").all_inner_texts()
            print("     课程:", " / ".join(tpl_names))
            shot(page, "02_logged_in")

            # 4) 选课 + 填表 + 快捷生成学号
            # 选「单表查询与聚合」（emp/dept 两表）：后面造错时 emp 确实存在，
            # 才能验证出"危险操作/写法提示"等分类，而不是全被判成表不存在
            target = page.locator("#tpl-grid .tpl").filter(has_text="单表查询")
            (target.first if target.count() else page.locator("#tpl-grid .tpl").first).click()
            page.wait_for_timeout(300)
            step("选中课程后卡片高亮", page.locator("#tpl-grid .tpl.sel").count() == 1)

            page.fill("#f-name", "E2E 冒烟实验")
            page.fill("#f-class", "E2E班")
            page.fill("#f-start", "981001")
            page.fill("#f-num", "2")
            page.click("button:has-text('填入名单')")
            ids = page.input_value("#f-stu").split()
            step("快捷生成学号", ids == ["981001", "981002"], str(ids))
            dep_n = page.inner_text("#dep-n")
            step("人数回显", "2" in dep_n, dep_n)

            # 5) 一键部署
            page.click("#btn-deploy")
            page.wait_for_function(
                "document.querySelector('#dep-ok').textContent.trim().length > 0",
                timeout=40000)
            msg = page.inner_text("#dep-ok")
            m = re.search(r"#(\d+)", msg)
            lab_id = int(m.group(1)) if m else None
            step("部署受理返回实验单号", lab_id is not None, msg[:60])
            shot(page, "03_deployed_accepted")

            # 6) 轮询到交付完成（页面自身每 5s 刷新，这里等它自己更新）
            print("  ... 等待容器交付（最多 4 分钟）")
            deadline = time.time() + 240
            card_text = ""
            while time.time() < deadline:
                card_text = _card_text(page, lab_id)
                if card_text and "交付中" not in card_text:
                    break
                page.wait_for_timeout(4000)
            found_card = bool(card_text)
            step("实验单卡片可定位", found_card, card_text[:50].replace("\n", " "))
            step("实验单交付完成（无『交付中』）",
                 found_card and "交付中" not in card_text)
            step("实例状态 running", "running" in card_text,
                 "" if found_card else "卡片未找到")
            shot(page, "04_lab_running")

            card = page.locator("#labs > div").filter(has_text=f"#{lab_id}").first
            if not found_card:
                raise RuntimeError("实验单卡片未渲染，后续导出/诊断无法进行")

            # 7) 导出 CSV（真实下载）
            try:
                with page.expect_download(timeout=40000) as dl_info:
                    card.locator("button:has-text('导出连接信息 CSV')").first.click()
                dl = dl_info.value
                csv_path = OUT / (dl.suggested_filename or f"lab_{lab_id}.csv")
                dl.save_as(str(csv_path))
                lines = [l for l in csv_path.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
                step("CSV 导出成功", len(lines) == 3, f"{len(lines)-1} 条数据 + 表头")
                creds = [l.split(",") for l in lines[1:]]
                print("     ", lines[0])
                print("     ", lines[1][:80] if len(lines) > 1 else "")
            except Exception as e:
                creds = []
                step("CSV 导出成功", False, str(e)[:120])

            # 8) 造错：用导出的凭据直连学生库，敲几条典型错误 SQL
            if creds:
                made = _inject_mistakes(creds)
                step("模拟学生失误（直连 2 个实例各敲 5 条）", made, "")

            # 9) 错题诊断
            card.locator("button:has-text('错题诊断')").first.click()
            try:
                page.wait_for_function(
                    f"""() => {{
                        const el = document.querySelector('#err-{lab_id}');
                        return el && el.innerText.includes('本轮新采集');
                    }}""", timeout=90000)
                diag = page.inner_text(f"#err-{lab_id}")
                step("错题诊断返回采集结果", "本轮新采集" in diag, diag.replace("\n", " ")[:70])
                found = [k for k in ("表不存在", "危险操作", "写法提示", "语法错误") if k in diag]
                step("诊断出具体失误类型", bool(found), " / ".join(found) or "未识别到分类")
                print("     诊断摘要:", diag.replace("\n", " ")[:220])
            except Exception as e:
                step("错题诊断返回采集结果", False, str(e)[:120])
            shot(page, "05_diagnosis")

        finally:
            # ---- 铁律 3：浏览器必须在 finally 关 ----
            browser.close()

    # ---- 铁律 2 的收尾：把监听到的错误全部入报告 ----
    print("\n== 运行时错误采集 ==")
    for label, items in (("pageerror 未捕获异常", page_errors),
                         ("console.error", console_errors),
                         ("HTTP 4xx/5xx", bad_http),
                         ("请求失败", failed_req)):
        print(f"  {label}: {len(items)}")
        for it in items[:8]:
            print("    -", it[:160])

    report = {
        "target": BASE,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lab_id": lab_id,
        "steps": steps,
        "runtime_errors": {
            "pageerror": page_errors,
            "console_error": console_errors,
            "http_error": bad_http,
            "request_failed": failed_req,
        },
        "screenshots": shots,
    }
    rp = OUT / "report.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [s for s in steps if not s["ok"]]
    print(f"\n== 汇总：{len(steps)-len(failed)}/{len(steps)} 通过，报告 {rp} ==")
    # JS 崩溃或接口报错即使断言全过也算失败
    hard_errors = len(page_errors) + len(console_errors) + len(bad_http)
    if failed or hard_errors:
        print("结论：不通过" + (f"（另有 {hard_errors} 条运行时/接口错误）" if hard_errors else ""))
        return 1
    print("结论：全部通过，浏览器无 JS 异常、无失败请求")
    return 0


def _inject_mistakes(creds: list[list[str]]) -> bool:
    """拿 CSV 里的凭据直连 MySQL，敲几条典型失误（模拟学生）。"""
    try:
        import pymysql
    except ImportError:
        print("     (未装 pymysql，跳过造错步骤)")
        return False

    bad_sql = [
        "SELECT * FROM emp",                       # 写法提示：SELECT *
        "SELECT empno FROM empp",                  # 表不存在
        "UPDATE emp SET salary = 0",               # 危险操作：无 WHERE
        "SELECT ename, salary FROM emp WHERE deptno = 10 ORDER BY salary DESC",  # 正确
        "SELECT dname, COUNT(*) FROM emp e JOIN dept d",  # JOIN 缺 ON
    ]
    done = 0
    for row in creds[:2]:
        if len(row) < 6:
            continue
        _sid, host, port, db, user, password = row[0], row[1], row[2], row[3], row[4], row[5]
        try:
            conn = pymysql.connect(host=host, port=int(port), user=user,
                                   password=password, database=db,
                                   connect_timeout=10, read_timeout=10)
            with conn.cursor() as cur:
                for sql in bad_sql:
                    try:
                        cur.execute(sql)
                        cur.fetchall()
                    except Exception:
                        pass  # 报错正是我们要的
            conn.commit()
            conn.close()
            done += 1
        except Exception as e:
            print(f"     连接 {host}:{port} 失败: {e}")
    return done > 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # 中断也要把已采集的运行时错误吐出来
        print(f"\n== 测试中断：{type(exc).__name__}: {str(exc)[:300]}")
        print("== 已采集的运行时错误 ==")
        for label, items in (("pageerror 未捕获异常", page_errors),
                             ("console.error", console_errors),
                             ("HTTP 4xx/5xx", bad_http),
                             ("请求失败", failed_req)):
            print(f"  {label}: {len(items)}")
            for it in items[:8]:
                print("    -", it[:200])
        sys.exit(1)
