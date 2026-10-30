"""任务 2 验收脚本：端口池并发租约与归还。

用法: python3.11 scripts/task2_test.py
验收标准（交接文档第八节任务 2）:
  并发租 5 个端口不重复；归还后可复用。
本脚本额外覆盖: 池耗尽、幂等归还、对账回收（防端口泄漏）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

# 用独立临时库，不污染正式元数据库（app.db）
_tmpdir = tempfile.mkdtemp(prefix="porttest_")
os.environ["APP_DB_PATH"] = os.path.join(_tmpdir, "app.db")

import sys
sys.path.insert(0, str(ROOT))

from app import config, ports  # noqa: E402

ports.init()

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    print(f"端口段: {config.PORT_RANGE_START}-{config.PORT_RANGE_END}  DB: {config.DB_PATH}\n")

    # ---- 1. 并发 5 线程租端口（验收标准①）----
    results: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        p = ports.lease_port(f"inst_{i}")
        with lock:
            results.append(p)

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(worker, range(5)))

    check("并发租 5 个端口全部成功", len(results) == 5, f"got {sorted(results)}")
    check("并发租端口无重复", len(set(results)) == 5, f"got {sorted(results)}")
    check("端口都在池段内", all(config.PORT_RANGE_START <= p <= config.PORT_RANGE_END for p in results))

    # ---- 2. 再加 5 线程，共 10 个仍不重复 ----
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(worker, range(5, 10)))
    check("10 个并发租约无重复", len(results) == 10 and len(set(results)) == 10)

    # ---- 3. 归还后可复用（验收标准②）----
    freed = results[3]
    check("归还端口返回 True", ports.release_port(freed) is True)
    check("重复归还幂等返回 False", ports.release_port(freed) is False)
    reused = ports.lease_port("inst_reuse")
    check("归还后可复用", reused == freed, f"released {freed}, re-leased {reused}")

    # ---- 4. 池耗尽 ----
    exhausted_ok = False
    try:
        # 租到只剩 0 个
        leased_now = set(ports.leased_ports())
        for i in range(100):
            p = ports.lease_port(f"fill_{i}")
            if p in leased_now:
                raise AssertionError(f"租到了已租端口 {p}")
    except ports.PortPoolExhausted:
        exhausted_ok = True
    check("租满后抛 PortPoolExhausted", exhausted_ok)
    check("100 个端口全部租出", len(ports.leased_ports()) == 100)

    # ---- 5. 按实例归还（回收整单场景）----
    freed_list = ports.release_by_instance("fill_0")
    check("按实例归还", len(freed_list) == 1 and ports.leased_ports().get(freed_list[0]) is None)

    # ---- 6. 对账：租约表有 99 条，实际只有 2 条在用 → 其余 97 条强制归还 ----
    actual = set(list(ports.leased_ports())[:2])
    stale = ports.reconcile(actual)
    check("对账回收泄漏端口", len(stale) == 97 and set(ports.leased_ports()) == actual,
          f"freed {len(stale)}")

    st = ports.stats()
    print(f"\n池状态: {st}")
    print("\n" + ("全部通过 ✓" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
