# -*- coding: utf-8 -*-
"""买入信号止损宽度 / 盈亏比分布统计（只读）——为 P1 盈亏比阈值 1.5 提供依据。

口径（两套参考价并列输出，主口径为 A）：
  A. 参考日收盘：信号 created_at 当日（= 纸面批次 reference_date，即成交日的
     前一交易日）的 stock_daily.close。取不到当日收盘时回退「created 之前最近
     一个交易日收盘」并单独计数。与执行器 ref_close 语义一致。
  B. 信号入场中价：(entry_low + entry_high)/2，缺一边取另一边。仅作对照。

指标：
  止损宽度 = (ref - stop_loss) / ref
  盈亏比 RR = (target_price - ref) / (ref - stop_loss)
  有效样本要求 stop>0、target>0、ref>0 且 stop < ref < target；
  不满足者单独归类（无止损 / 无止盈 / 价格倒挂），不参与 RR 分位数。

拦截评估：RR < 阈值（1.2 / 1.5 / 2.0）的信号占比；并列出账户 2 实际买入过
且会被 1.5 拦截的信号。

用法：python docs/策略优化20261116/调研/统计_阈值依据.py
硬约束：mode=ro + query_only，零写入；纯标准库。
"""
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"
THRESHOLDS = [1.2, 1.5, 2.0]


def connect_ro():
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def quantiles(vals):
    if not vals:
        return {}
    s = sorted(vals)
    def q(p):
        idx = p * (len(s) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)
    return {"min": s[0], "p10": q(.1), "p25": q(.25), "中位": q(.5),
            "p75": q(.75), "p90": q(.9), "max": s[-1]}


def fmt_q(d, pct=False):
    if not d:
        return "（无样本）"
    f = (lambda v: f"{v * 100:.2f}%") if pct else (lambda v: f"{v:.2f}")
    return "  ".join(f"{k}={f(v)}" for k, v in d.items())


def ref_close_a(con, code, created_day):
    r = con.execute("SELECT close FROM stock_daily WHERE code=? AND date=?",
                    (code, created_day)).fetchone()
    if r and r["close"]:
        return float(r["close"]), False
    r = con.execute("SELECT close FROM stock_daily WHERE code=? AND date<? "
                    "ORDER BY date DESC LIMIT 1", (code, created_day)).fetchone()
    return (float(r["close"]), True) if r and r["close"] else (None, False)


def analyze(con, label, ref_getter):
    rows = con.execute(
        "SELECT id, stock_code, horizon, score, entry_low, entry_high, "
        "stop_loss, target_price, created_at FROM decision_signals "
        "WHERE action='buy' ORDER BY id"
    ).fetchall()
    widths, rrs, records = [], [], []
    miss_stop = miss_target = miss_ref = inverted = fallback = 0
    for s in rows:
        created_day = (s["created_at"] or "")[:10]
        ref, fb = ref_getter(con, s, created_day)
        fallback += 1 if fb else 0
        stop, target = s["stop_loss"], s["target_price"]
        if not stop or stop <= 0:
            miss_stop += 1
            continue
        if not target or target <= 0:
            miss_target += 1
            continue
        if not ref or ref <= 0:
            miss_ref += 1
            continue
        if not (stop < ref < target):
            inverted += 1
            records.append((s, ref, None, None))
            continue
        w = (ref - stop) / ref
        rr = (target - ref) / (ref - stop)
        widths.append(w)
        rrs.append(rr)
        records.append((s, ref, w, rr))

    n = len(rows)
    valid = len(rrs)
    print(f"\n{'=' * 72}\n口径 {label}\n{'=' * 72}")
    print(f"买入信号总数={n}（horizon 分布: "
          + ", ".join(f"{h}×{c}" for h, c in con.execute(
              "SELECT horizon, COUNT(*) FROM decision_signals WHERE action='buy' "
              "GROUP BY horizon").fetchall()) + "）")
    print(f"剔除: 无止损={miss_stop}  无止盈={miss_target}  无参考价={miss_ref}  "
          f"价格倒挂(stop>=ref 或 target<=ref)={inverted}"
          + (f"  [参考价回退前一交易日={fallback}]" if fallback else ""))
    print(f"有效样本={valid}")
    print(f"止损宽度分布: {fmt_q(quantiles(widths), pct=True)}")
    buckets = {"<3%": 0, "3~4%": 0, "4~5%": 0, "5~6%": 0, "6~8%": 0, ">8%": 0}
    for w in widths:
        p = w * 100
        k = "<3%" if p < 3 else "3~4%" if p < 4 else "4~5%" if p < 5 else \
            "5~6%" if p < 6 else "6~8%" if p < 8 else ">8%"
        buckets[k] += 1
    print("止损宽度分桶: " + "  ".join(
        f"{k}:{v}({v / valid * 100:.1f}%)" for k, v in buckets.items()))
    print(f"盈亏比 RR 分布: {fmt_q(quantiles(rrs))}")
    rb = {"<1.0": 0, "1.0~1.5": 0, "1.5~2.0": 0, "2.0~3.0": 0, ">=3.0": 0}
    for rr in rrs:
        k = "<1.0" if rr < 1 else "1.0~1.5" if rr < 1.5 else \
            "1.5~2.0" if rr < 2 else "2.0~3.0" if rr < 3 else ">=3.0"
        rb[k] += 1
    print("RR 分桶: " + "  ".join(
        f"{k}:{v}({v / valid * 100:.1f}%)" for k, v in rb.items()))
    for th in THRESHOLDS:
        blocked = sum(1 for rr in rrs if rr < th)
        print(f"阈值 {th}: 拦截 {blocked}/{valid} = {blocked / valid * 100:.1f}% "
              f"（占全部买入信号 {blocked / n * 100:.1f}%）")
    return records


def main():
    con = connect_ro()
    print(f"数据库(只读): {DB_PATH}")

    def ref_a(con, s, created_day):
        return ref_close_a(con, s["stock_code"], created_day)

    def ref_b(con, s, created_day):
        lo, hi = s["entry_low"], s["entry_high"]
        if lo and hi:
            return (lo + hi) / 2, False
        return (hi or lo or None), False

    rec_a = analyze(con, "A（主口径）：参考日收盘 = 信号 created 当日收盘", ref_a)
    analyze(con, "B（对照）：信号入场中价 (entry_low+entry_high)/2", ref_b)

    # 账户 2 实际买入信号 × 1.5 阈值
    print(f"\n{'=' * 72}\n账户 2 实际买入信号在口径 A 下的 RR（阈值 1.5）\n{'=' * 72}")
    bought = {}
    for t in con.execute(
        "SELECT symbol, trade_date, price, quantity, note FROM portfolio_trades "
        "WHERE account_id=2 AND side='buy' ORDER BY id"
    ).fetchall():
        import re
        m = re.search(r"signal #(\d+)", t["note"] or "")
        if m:
            bought[int(m.group(1))] = t
    hit = 0
    for s, ref, w, rr in rec_a:
        if s["id"] not in bought:
            continue
        t = bought[s["id"]]
        if rr is None:
            print(f"  信号#{s['id']} {s['stock_code']} {t['trade_date']} "
                  f"买{t['quantity']:.0f}股@{t['price']}: 价格倒挂/缺失，RR 无法计算")
            continue
        flag = "← 会被 1.5 拦截" if rr < 1.5 else ""
        hit += 1 if rr < 1.5 else 0
        print(f"  信号#{s['id']} {s['stock_code']} {t['trade_date']} "
              f"买{t['quantity']:.0f}股@{t['price']}  ref={ref:.2f} "
              f"止损宽度={w * 100:.2f}% RR={rr:.2f} {flag}")
    print(f"  实际买入 {len(bought)} 笔中被 1.5 拦截: {hit} 笔")
    con.close()
    print("\n统计完成：全程只读，未写生产库。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()