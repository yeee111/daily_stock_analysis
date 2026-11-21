# -*- coding: utf-8 -*-
"""只读诊断：DSA 晚间定时分析 worker_lost 根因复核脚本。

用法（PowerShell）：
  & "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" `
    "D:\\qoder-D\\qoder\\daily_stock_analysis\\docs\\策略优化20261116\\调研\\诊断_worker退出挂起.py"

硬约束：
  - 不导入任何项目模块、不触发分析、不写生产库（sqlite 一律 mode=ro）；
  - Part A 的 spawn 子进程是本脚本自建的隔离实验进程（只挂一个 Event.wait 线程），
    实验结束后由本脚本自行 terminate/kill 清理，与生产进程无关；
  - Part B 仅 GET 调度器状态；Part C/D 仅读库/读日志。

四个部分：
  A. 隔离复现"shutdown(wait=False) 遗留非 daemon 线程 → 解释器退出被 atexit 无限 join"
     机制（Python 3.9+ 标准库行为，与生产子进程同构），并给无挂起线程的对照基线。
  B. GET /api/v1/system/scheduler/status，读 last_error（worker_lost 两条路径的判别字符串）。
  C. 只读对照 analysis_history（本地时间）与 decision_signals（UTC）入库时序，
     验证"分析实际完成 + 信号实际产生（UTC 误读）"。
  D. 扫描日志 sinks 中全部 worker_lost 事件，输出日期/时刻清单。
"""
from __future__ import annotations

import multiprocessing
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # docs/策略优化20261116/调研 -> 仓库根
PYTHON = sys.executable


# ---------------------------------------------------------------- Part A
def _child_with_hung_thread(q):
    """模拟 efinance _ef_call_with_timeout / agent runner 超时路径的遗留线程。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    never = threading.Event()
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(never.wait)          # 模拟无超时网络读：永远不返回
    ex.shutdown(wait=False)        # 与生产代码一致的"不等待"收尾
    q.put({"analysis_done": True}) # 模拟 result_queue.put：业务已完成
    # 之后自然退出 → threading._register_atexit(_python_exit) 无限 join 挂起线程


def _child_clean(q):
    q.put({"analysis_done": True})


def part_a():
    print("=" * 60)
    print("Part A: 隔离复现退出挂起机制（自建实验进程，不涉及生产）")
    ctx = multiprocessing.get_context("spawn")
    for name, target in (("遗留挂起线程", _child_with_hung_thread), ("对照:无遗留线程", _child_clean)):
        q = ctx.Queue()
        p = ctx.Process(target=target, args=(q,), name=f"diag-{name}")
        p.start()
        try:
            res = q.get(timeout=60)
            t0 = time.monotonic()
            p.join(5)
            elapsed = time.monotonic() - t0
            if p.is_alive():
                print(f"  [{name}] 结果已收到={res}；join(5) 后仍存活"
                      f"（exitcode={p.exitcode}）→ 退出被 atexit join 挂起，复现成功")
                p.terminate()
                p.join(5)
                if p.is_alive():
                    p.kill()
                    p.join(5)
                print(f"  [{name}] 实验进程已清理（terminate 对应生产 watchdog 的 taskkill）")
            else:
                print(f"  [{name}] 结果已收到；{elapsed:.2f}s 内正常退出，exitcode={p.exitcode}")
        finally:
            q.cancel_join_thread()
            q.close()
    print("  结论判读：挂起组存活 + 对照组秒退 => join(2) 误杀机制成立（标准库行为，非项目代码崩溃）")


# ---------------------------------------------------------------- Part B
def part_b():
    print("=" * 60)
    print("Part B: 调度器状态（只读 GET）")
    url = "http://127.0.0.1:8000/api/v1/system/scheduler/status"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=10) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
        for k in ("enabled", "running", "last_run_at", "last_success_at", "last_error"):
            print(f"  {k}: {data.get(k)}")
        err = data.get("last_error") or ""
        if "did not exit" in err:
            print("  => 命中路径B（L722-729）：结果已入队但 join(2) 超时被强杀（本次根因）")
        elif "without a result" in err:
            print("  => 命中路径A（L710-720）：子进程未 put 结果即退出（另一类，需另查 exitcode）")
    except Exception as exc:
        print(f"  查询失败（服务未运行或端口变化）: {exc}")


# ---------------------------------------------------------------- Part C
def part_c():
    print("=" * 60)
    print("Part C: 只读 DB 时序对照（analysis_history=本地时间, decision_signals=UTC）")
    db = REPO / "data" / "stock_analysis.db"
    if not db.exists():
        print(f"  数据库不存在: {db}")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        print("  -- 11-17/11-18 晚间分析最后入库 vs worker_lost 时刻 --")
        for day, lost in (("2026-11-17", "18:32:15"), ("2026-11-18", "18:34:14")):
            row = con.execute(
                "SELECT created_at, code FROM analysis_history "
                "WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC LIMIT 1",
                (f"{day} 18:00", f"{day} 19:00"),
            ).fetchone()
            n = con.execute(
                "SELECT count(*) FROM analysis_history WHERE created_at >= ? AND created_at < ?",
                (f"{day} 18:00", f"{day} 19:00"),
            ).fetchone()[0]
            print(f"  {day}: 晚间入库 {n} 条，最后一条 {row}；worker_lost {lost}"
                  f" => 分析跑完后 ~2-4s 被杀" if row else f"  {day}: 无晚间入库")
        print("  -- decision_signals 按日计数（created_at 为 UTC，+8h 即本地晚间批） --")
        for row in con.execute(
            "SELECT substr(created_at,1,10) d, trigger_source, count(*), "
            "min(substr(created_at,12,5)), max(substr(created_at,12,5)) "
            "FROM decision_signals WHERE created_at >= '2026-11-16' GROUP BY 1,2 ORDER BY 1"
        ):
            d, src, n, lo, hi = row
            lo_l = (datetime.fromisoformat(f"{d} {lo}") + timedelta(hours=8)).strftime("%H:%M")
            hi_l = (datetime.fromisoformat(f"{d} {hi}") + timedelta(hours=8)).strftime("%H:%M")
            print(f"  {d} [{src}] {n} 条: UTC {lo}-{hi} = 本地 {lo_l}-{hi_l}（晚间批，非早间）")
        print("  -- 4ms 级耦合证据：信号 source_report_id 指向晚间报告 --")
        for row in con.execute(
            "SELECT s.created_at, s.stock_code, s.source_report_id, h.created_at "
            "FROM decision_signals s JOIN analysis_history h ON h.id = s.source_report_id "
            "WHERE s.created_at >= '2026-11-18' ORDER BY s.created_at DESC LIMIT 3"
        ):
            print(f"  信号(UTC)={row[0]} {row[1]} → 报告(本地)={row[3]} (id={row[2]})")
    finally:
        con.close()


# ---------------------------------------------------------------- Part D
def part_d():
    print("=" * 60)
    print("Part D: 日志 sinks 中全部 worker_lost 事件")
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*outcome: worker_lost")
    seen = set()
    sinks = [REPO / "data" / "webui_startup.log"]
    sinks += sorted((REPO / "logs").glob("stock_analysis_2*.log"))
    for f in sinks:
        if not f.exists():
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = pat.match(line)
                    if m and m.group(1) not in seen:
                        seen.add(m.group(1))
                        print(f"  {m.group(1)}  ({f.name})")
        except OSError as exc:
            print(f"  读取失败 {f}: {exc}")
    print(f"  共 {len(seen)} 个独立事件；若全部落在 18:3x => 与'分析时长~30min + join(2)'吻合")


if __name__ == "__main__":
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    print(f"仓库根: {REPO}")
    print(f"Python: {PYTHON} ({sys.version.split()[0]})")
    part_a()
    part_b()
    part_c()
    part_d()
    print("=" * 60)
    print("诊断完成（全程只读；Part A 实验进程已自行清理）")