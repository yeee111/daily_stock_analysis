# -*- coding: utf-8 -*-
"""回补机制审计（只读）

对纸面账户 2 的三笔二次进场（600276 / 002415 / 600028）做 signal id 级证据链审计，
判定每次回补是「旧信号 carried 顺延」还是「新信号」。

证据来源（全部只读）：
  - portfolio_trades.note 里的 "signal #id"
  - decision_signals（created_at / expires_at / action / horizon / 价格三元组）
  - paper_account_runs.decisions_json（carried / due_signal_ids / signal_ids，若存在）
  - stock_daily（sh000300 交易日历 + 参考日收盘价，用于重建现金/单票预算）

判定规则（与 src/services/paper_trading_service.py 语义对齐）：
  - _bucket_by_fill（L841-859）：信号成交日 = created_at 日期的下一交易日。
  - _merge_carried_signals（L2393-2441）：carried 只顺延「原成交日因涨跌停/停牌
    无法成交」的旧信号，且保留原 signal id；原成交日已成交（trade_uid 已存在）
    或中间有可成交却漏跑的交易日，都不顺延。
  - 因此：回补买单的 signal id 若是一条 created_at 晚于前次出场的新信号，
    且 实际成交日 == 该信号的预期成交日（下一交易日），即为「新信号」，非 carried。

用法（仓库根目录或任意目录均可）：
  python docs/策略优化20261116/调研/审计_回补机制.py

硬约束：对 data/stock_analysis.db 以 SQLite URI mode=ro 打开，零写入。
"""
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"
ACCOUNT_ID = 2
INITIAL_CASH = 100_000.0  # 任务书基线：2026-10-30 初始 10 万
MAX_NAMES = 10           # .env PAPER_TRADING_MAX_NAMES=10
MAX_WEIGHT = 0.2         # 内置默认 DEFAULT_MAX_WEIGHT（.env 未覆盖）
TARGETS = ["600276", "002415", "600028"]


def connect_ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # 双保险：拒绝任何写语句
    con.execute("PRAGMA query_only = ON")
    return con


def trading_calendar(con) -> list:
    rows = con.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE code='sh000300' ORDER BY date"
    ).fetchall()
    return [r["date"] for r in rows]


def next_trading_day(calendar: list, day: str):
    for d in calendar:
        if d > day:
            return d
    return None


def prev_trading_day(calendar: list, day: str):
    prev = None
    for d in calendar:
        if d >= day:
            break
        prev = d
    return prev


def signal_id_from_note(note: str):
    m = re.search(r"signal #(\d+)", note or "")
    return int(m.group(1)) if m else None


def fetch_signal(con, sid: int):
    return con.execute(
        "SELECT id, stock_code, action, horizon, score, entry_low, entry_high, "
        "stop_loss, target_price, status, created_at, expires_at "
        "FROM decision_signals WHERE id=?",
        (sid,),
    ).fetchone()


def fetch_runs_for_fill(con, fill_date: str):
    rows = con.execute(
        "SELECT id, batch_key, status, decisions_json, created_at "
        "FROM paper_account_runs WHERE account_id=? AND batch_key LIKE ? ORDER BY id",
        (ACCOUNT_ID, f"cn-{fill_date}-%"),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["decisions_json"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append({
            "run_id": r["id"], "status": r["status"], "created_at": r["created_at"],
            "carried": payload.get("carried"),
            "due_signal_ids": payload.get("due_signal_ids"),
            "signal_ids": payload.get("signal_ids"),
        })
    return out


def close_on(con, code: str, day: str):
    r = con.execute(
        "SELECT close FROM stock_daily WHERE code=? AND date=?", (code, day)
    ).fetchone()
    return float(r["close"]) if r and r["close"] else None


def clamp_lot(qty: int) -> int:
    return (qty // 100) * 100 if qty >= 100 else 0


def audit_stock(con, calendar, code):
    print(f"\n{'=' * 72}\n【{code}】成交流水与信号链（账户 {ACCOUNT_ID}）\n{'=' * 72}")
    trades = con.execute(
        "SELECT id, side, trade_date, price, quantity, fee, tax, note, created_at "
        "FROM portfolio_trades WHERE account_id=? AND symbol=? ORDER BY trade_date, id",
        (ACCOUNT_ID, code),
    ).fetchall()
    buys = []
    for t in trades:
        sid = signal_id_from_note(t["note"])
        sig = fetch_signal(con, sid) if sid else None
        created_day = (sig["created_at"] or "")[:10] if sig else None
        expected_fill = next_trading_day(calendar, created_day) if created_day else None
        carried_guess = (
            expected_fill is not None and t["trade_date"] != expected_fill
        )
        print(f"  成交#{t['id']} {t['trade_date']} {t['side']:4s} "
              f"{t['quantity']:>6.0f}股 @ {t['price']:<7} note={t['note']!r} "
              f"(落库 {t['created_at'][:19]})")
        if sig is None:
            print("    !! note 中解析不到 signal id 或信号行不存在")
            continue
        print(f"    信号#{sig['id']}: action={sig['action']} horizon={sig['horizon']} "
              f"score={sig['score']} created={sig['created_at'][:19]} "
              f"expires={sig['expires_at'][:19]} status={sig['status']}")
        print(f"    价格: entry=[{sig['entry_low']}, {sig['entry_high']}] "
              f"stop={sig['stop_loss']} target={sig['target_price']}")
        print(f"    预期成交日(created 下一交易日)={expected_fill} "
              f"实际成交日={t['trade_date']} "
              f"→ {'日期不符，需查 carried' if carried_guess else '当日到期批次，非顺延'}")
        runs = fetch_runs_for_fill(con, t["trade_date"])
        if not runs:
            print(f"    paper_account_runs: 无 fill={t['trade_date']} 的审计行"
                  f"（该批次早于审计落库功能/或行缺失，以信号链+日历判定）")
        for run in runs:
            in_due = run["due_signal_ids"] and sig["id"] in run["due_signal_ids"]
            in_carried = bool(run["carried"]) and code in (run["carried"] or {})
            print(f"    run#{run['run_id']} status={run['status']} "
                  f"carried={run['carried']} "
                  f"信号#{sig['id']}∈due_signal_ids: {in_due} / ∈carried: {in_carried}")
        if t["side"] == "buy":
            buys.append((t, sig, expected_fill, carried_guess))

    # 二次进场判定
    print(f"\n  —— {code} 回补判定 ——")
    for i in range(1, len(buys)):
        t, sig, expected_fill, carried_guess = buys[i]
        prev_t, prev_sig, _, _ = buys[i - 1]
        # 前次出场：两笔买入之间是否存在卖出
        sells_between = [x for x in trades
                         if x["side"] == "sell"
                         and prev_t["trade_date"] <= x["trade_date"] <= t["trade_date"]]
        new_id = sig["id"] != prev_sig["id"]
        created_after_prev = (sig["created_at"] or "") > (prev_sig["created_at"] or "")
        verdict = ("新信号（非 carried）"
                   if new_id and created_after_prev and not carried_guess
                   else "carried 顺延嫌疑，需人工复核")
        print(f"  第{i + 1}次进场 成交#{t['id']} {t['trade_date']} "
              f"{t['quantity']:.0f}股@{t['price']} ← 信号#{sig['id']}")
        print(f"    前次买入信号#{prev_sig['id']}"
              f"(created {prev_sig['created_at'][:19]})，"
              f"其间卖出 {len(sells_between)} 笔"
              + (f"（{sells_between[0]['trade_date']} @ {sells_between[0]['price']}）"
                 if sells_between else ""))
        print(f"    信号 id 不同: {new_id}；新信号 created 晚于旧信号: {created_after_prev}；"
              f"预期成交日==实际成交日: {not carried_guess}")
        print(f"    结论: {verdict}")


def cash_context(con, calendar):
    """按流水重建现金，解释 600028 量 100→1700（预算=单票目标 vs 现金×0.995）。"""
    print(f"\n{'=' * 72}\n【现金/单票预算重建】初始 {INITIAL_CASH:.0f} 元，"
          f"per_name = 权益 × min(1/max_names={MAX_NAMES}, max_weight={MAX_WEIGHT})\n{'=' * 72}")
    trades = con.execute(
        "SELECT id, symbol, side, trade_date, price, quantity, fee, tax "
        "FROM portfolio_trades WHERE account_id=? ORDER BY id", (ACCOUNT_ID,)
    ).fetchall()
    cash = INITIAL_CASH
    held = {}
    for t in trades:
        code, qty = t["symbol"], int(t["quantity"])
        gross = t["price"] * qty
        fee, tax = t["fee"] or 0.0, t["tax"] or 0.0
        if t["side"] == "buy" and code == "600028":
            ref_day = prev_trading_day(calendar, t["trade_date"])
            ref_close = close_on(con, code, ref_day) if ref_day else None
            equity = cash + sum(
                q * (close_on(con, c, ref_day) or 0.0) for c, q in held.items() if q > 0
            )
            per_name = equity * min(1.0 / MAX_NAMES, MAX_WEIGHT)
            target = clamp_lot(int(per_name / ref_close)) if ref_close else None
            affordable = clamp_lot(int(cash * 0.995 / t["price"]))
            print(f"  成交#{t['id']} {t['trade_date']} 买 {code} 前: 现金={cash:.2f} "
                  f"权益≈{equity:.2f} 参考日({ref_day})收盘={ref_close}")
            print(f"    单票目标 per_name≈{per_name:.2f} → 目标量={target} 股；"
                  f"现金可买 affordable_qty={affordable} 股；实际={qty} 股")
        if t["side"] == "buy":
            cash -= gross + fee + tax
            held[code] = held.get(code, 0) + qty
        else:
            cash += gross - fee - tax
            held[code] = held.get(code, 0) - qty
    print(f"  期末现金={cash:.2f}")


def main():
    if not DB_PATH.exists():
        print(f"找不到数据库: {DB_PATH}")
        sys.exit(1)
    con = connect_ro()
    calendar = trading_calendar(con)
    print(f"数据库(只读): {DB_PATH}")
    print(f"交易日历: sh000300 共 {len(calendar)} 个交易日 "
          f"({calendar[0]} ~ {calendar[-1]})")
    n_runs = con.execute(
        "SELECT COUNT(*) c FROM paper_account_runs WHERE account_id=?", (ACCOUNT_ID,)
    ).fetchone()["c"]
    statuses = con.execute(
        "SELECT status, COUNT(*) c FROM paper_account_runs WHERE account_id=? GROUP BY status",
        (ACCOUNT_ID,),
    ).fetchall()
    print(f"paper_account_runs(账户{ACCOUNT_ID}): 共 {n_runs} 行，状态分布 "
          + ", ".join(f"{r['status']}×{r['c']}" for r in statuses))
    for code in TARGETS:
        audit_stock(con, calendar, code)
    cash_context(con, calendar)
    con.close()
    print("\n审计完成：全程只读（mode=ro + query_only），未写生产库。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()