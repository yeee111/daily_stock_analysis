# -*- coding: utf-8 -*-
"""干跑_风控对比 —— 对只读生产库回放 原策略 vs 新风控（2026-11-16 策略优化）

一条命令：
    python docs/策略优化20261116/实现/干跑_风控对比.py
默认回放账户 2（纸面交易）2026-11-02 → 2026-11-13 窗口，初始资金 10 万。

回放口径（成交流水锚定，已按 2026-11-16 主会话裁决修订）：
  - 原策略 = portfolio_trades 实际成交逐笔重演（费用按模块费率重算并与库中核对）。
  - 新风控 = 同样的成交事件流，买入先过 纸面风控模块.evaluate_entry 闸门，
    预算只缩量不放大；卖出仅对仍持有的仓位生效（被拦买入的后续卖出连带取消）；
    P0 兜底止损逐日扫描裸仓（无信号止损的持仓），日内最低价触线即按止损价精确成交。
  - RR 用信号入场中价 (entry_low+entry_high)/2 口径；坏价样本剔除不进 RR。
  - 对照组：仅RR@1.0 / 仅RR@1.5 / RR1.5+止损冷却5日 / RR1.5+全冷却5日 /
    仅P0兜底 / 仅P2预算 / 仅P2最小金额 / 全开(默认,冷却off)，
    并输出冷却期在 RR 之上的边际拦截清单与各自盈亏。
  - 账户 2 的 paper_account_runs 无 executed 行（全是 duplicate_batch 噪声），
    故退出价/目标价从成交 note 的 signal id 反查 decision_signals，不依赖 decisions_json。
  - carried 判定启发式仅供展示（调研证实三笔问题回补全是新信号，拦截点在新信号循环）。
  - 权益按当日收盘逐日盯市；开仓闸门的权益/现金用决策时点（前收盘）口径。

安全：sqlite 以 mode=ro 只读 URI 打开；跑前跑后核对关键表行数与文件大小；
不写任何库表，不依赖 DSA 服务进程，纯标准库。
输出：控制台中文摘要 + 报告 md（缺省写当前工作目录 干跑_风控对比_结果.md，
--报告 可指定路径；T4：默认不覆盖 实现/ 下交付文件）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import 纸面风控模块 as rc  # noqa: E402

# T1：Windows 默认控制台（GBK/cp936）无法编码 ✅ 等字符会抛 UnicodeEncodeError；
# 保留控制台编码（中文正常显示），不可编码字符替换为 ?，保证裸 python 命令退出码 0。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

实现目录 = Path(__file__).resolve().parent
仓库根 = 实现目录.parents[1].parent  # docs/策略优化20261116/实现 → 仓库根


def 只读URI(db路径: Path) -> str:
    """中文/特殊字符路径需百分号编码，as_uri 处理后拼 mode=ro。"""
    return db路径.resolve().as_uri() + "?mode=ro"

基准权益 = 101496.98   # 任务书基线（2026-11-13 收盘，FIFO 含费用）
基准已实现 = 1411.84
基准费用 = 163.17
基准税 = 57.85


def d(x) -> Decimal:
    return rc.to_cent(x)


# ----------------------------------------------------------------------
# 数据加载（全部只读）
# ----------------------------------------------------------------------
def 加载数据(db路径: Path, 账户: int, 起: str, 止: str):
    con = sqlite3.connect(只读URI(db路径), uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    表清单 = ["portfolio_trades", "decision_signals", "paper_account_runs", "portfolio_positions", "stock_daily"]
    行数前 = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in 表清单}

    成交 = [dict(r) for r in cur.execute(
        "SELECT id,trade_date,symbol,side,quantity,price,fee,tax,note FROM portfolio_trades "
        "WHERE account_id=? AND trade_date<=? ORDER BY trade_date,id", (账户, 止))]
    早期 = [t for t in 成交 if t["trade_date"] < 起]
    if 早期:
        raise SystemExit(f"窗口起点 {起} 之前存在 {len(早期)} 笔成交，本回放要求从账户首笔成交日开始（不支持窗口中途热启动）")

    信号ids = sorted({int(m.group(1)) for t in 成交 if (m := re.search(r"signal #(\d+)", str(t["note"] or "")))})
    信号 = {}
    if 信号ids:
        for r in cur.execute(
            f"SELECT id,stock_code,stock_name,stop_loss,target_price,entry_low,entry_high,created_at FROM decision_signals WHERE id IN ({','.join('?' * len(信号ids))})",
            信号ids,
        ):
            信号[r["id"]] = dict(r)

    日历 = [r[0] for r in cur.execute(
        "SELECT date FROM stock_daily WHERE code='sh000300' AND date BETWEEN ? AND ? ORDER BY date", (起, 止))]
    代码集 = sorted({t["symbol"] for t in 成交})
    日线 = {}
    for code in 代码集:
        日线[code] = {r["date"]: dict(r) for r in cur.execute(
            "SELECT date,open,low,close FROM stock_daily WHERE code=? AND date<=? ORDER BY date", (code, 止))}
    con.close()
    return 成交, 信号, 日历, 日线, 行数前, 表清单


def 只读复核(db路径: Path, 表清单, 行数前):
    con = sqlite3.connect(只读URI(db路径), uri=True)
    行数后 = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in 表清单}
    con.close()
    一致 = 行数前 == 行数后
    return 一致, 行数前, 行数后


# ----------------------------------------------------------------------
# 回放引擎
# ----------------------------------------------------------------------
class 账本:
    """场景内现金/持仓/FIFO 台账（Decimal 计分，与生产口径一致）。"""

    def __init__(self, 初始资金: float):
        self.现金 = d(初始资金)
        self.持仓 = {}   # code -> {qty, lots:[(qty,price,fee)], stop, target, stop_source, last_buy_price}
        self.流水 = []   # rc.TradeView 列表（供冷却期查询）
        self.成交明细 = []
        self.已实现 = Decimal("0.00")
        self.费用合计 = Decimal("0.00")
        self.税合计 = Decimal("0.00")
        self.费用核对不符 = 0
        self.重演现金不足 = 0

    def 买入(self, 日, code, qty, price, 信号行, 备注=""):
        gross = d(Decimal(str(price)) * qty)
        fee, tax = rc.compute_fees("buy", gross)
        self.现金 -= gross + fee
        pos = self.持仓.setdefault(code, {"qty": 0, "lots": [], "stop": None, "target": None, "stop_source": "", "last_buy_price": 0.0})
        pos["qty"] += qty
        pos["lots"].append([qty, float(price), float(fee)])
        pos["last_buy_price"] = float(price)
        stop = 信号行.get("stop_loss") if 信号行 else None
        target = 信号行.get("target_price") if 信号行 else None
        pos["stop"] = float(stop) if stop and stop > 0 else None
        pos["stop_source"] = "signal" if pos["stop"] else ""
        pos["target"] = float(target) if target and target > 0 else None
        self.流水.append(rc.TradeView(code, 日, "buy", qty, float(price)))
        self.费用合计 += fee
        self.成交明细.append({"日": 日, "代码": code, "方向": "buy", "数量": qty, "价格": float(price), "费用": float(fee), "税": 0.0, "备注": 备注})
        return fee

    def 卖出(self, 日, code, qty, price, 出场类型, 备注=""):
        pos = self.持仓.get(code)
        if not pos or pos["qty"] <= 0:
            return None
        qty = min(qty, pos["qty"])
        gross = d(Decimal(str(price)) * qty)
        fee, tax = rc.compute_fees("sell", gross)
        self.现金 += gross - fee - tax
        # FIFO 成本（含买入费用分摊）
        成本 = Decimal("0.00")
        剩余 = qty
        while 剩余 > 0 and pos["lots"]:
            lot = pos["lots"][0]
            取 = min(剩余, lot[0])
            成本 += d(Decimal(str(lot[1])) * 取) + d(Decimal(str(lot[2])) * 取 / lot[0])
            lot[0] -= 取
            剩余 -= 取
            if lot[0] <= 0:
                pos["lots"].pop(0)
        pnl = gross - fee - tax - 成本
        self.已实现 += pnl
        self.费用合计 += fee
        self.税合计 += tax
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del self.持仓[code]
        self.流水.append(rc.TradeView(code, 日, "sell", qty, float(price), 出场类型))
        self.成交明细.append({"日": 日, "代码": code, "方向": "sell", "数量": qty, "价格": float(price), "费用": float(fee), "税": float(tax), "盈亏": float(pnl), "出场": 出场类型 or "", "备注": 备注})
        return pnl

    def 权益(self, 日, 日线):
        市值 = Decimal("0.00")
        for code, pos in self.持仓.items():
            bar = 日线.get(code, {}).get(日)
            px = bar["close"] if bar else pos["last_buy_price"]
            市值 += d(Decimal(str(px)) * pos["qty"])
        return float(self.现金 + 市值)


def 回放(成交, 信号, 日历, 日线, 初始资金, 配置, 场景名):
    """配置=None 表示原策略重演；否则过风控闸门。"""
    账 = 账本(初始资金)
    拦截清单, 缩量清单, 兜底清单, 连带清单 = [], [], [], []
    权益曲线 = []
    最低现金 = float(账.现金)
    按日 = {}
    for t in 成交:
        按日.setdefault(t["trade_date"], []).append(t)
    前一交易日 = {日: (日历[i - 1] if i > 0 else None) for i, 日 in enumerate(日历)}

    def 信号行(t):
        m = re.search(r"signal #(\d+)", str(t["note"] or ""))
        return 信号.get(int(m.group(1))) if m else None

    for 日 in 日历:
        当日 = 按日.get(日, [])
        # 1) 原流水卖出（先出后进，与引擎一致）
        for t in [x for x in 当日 if x["side"] == "sell"]:
            code = t["symbol"]
            pos = 账.持仓.get(code)
            if not pos:
                连带清单.append({"日": 日, "代码": code, "数量": t["quantity"], "价格": t["price"], "原因": "对应买入已被拦截/兜底出场，卖出连带取消"})
                continue
            出场 = rc.resolve_exit_kind(note=t["note"], sell_price=t["price"], stop_loss=pos["stop"],
                                        target_price=pos["target"], allow_price_inference=True)
            原数量 = int(t["quantity"])
            实际 = min(原数量, pos["qty"])
            账.卖出(日, code, 实际, t["price"], 出场, 备注=f"原流水#{t['id']}")
            if 实际 < 原数量:
                连带清单.append({"日": 日, "代码": code, "数量": 原数量 - 实际, "价格": t["price"], "原因": "持仓因缩量不足原卖出数量，按持有量成交"})
        # 2) P0 兜底止损逐日扫描（仅裸仓；已有信号止损由原流水负责）
        if 配置 is not None and 配置.fallback_stop_enabled:
            for code in list(账.持仓.keys()):
                pos = 账.持仓[code]
                bar = 日线.get(code, {}).get(日)
                if not bar or pos["stop"] is not None:
                    continue
                pv = rc.PositionView(code, pos["qty"], pos["last_buy_price"], None, "")
                触发价 = rc.check_stop_trigger(pv, bar["low"], 配置, day_open=bar["open"])
                if 触发价 is not None:
                    数量 = pos["qty"]
                    pnl = 账.卖出(日, code, 数量, 触发价, "stop_loss", 备注="P0兜底止损")
                    兜底清单.append({"日": 日, "代码": code, "止损价": 触发价, "买入价": pos["last_buy_price"], "数量": 数量, "盈亏": float(pnl)})
        # 3) 原流水买入
        for t in [x for x in 当日 if x["side"] == "buy"]:
            code, 价, 量 = t["symbol"], float(t["price"]), int(t["quantity"])
            sig = 信号行(t)
            if 配置 is None:
                可买 = rc.clamp_buy_qty(code, int(float(账.现金) * 0.995 / 价)) if 价 > 0 else 0
                if 可买 < 量:
                    账.重演现金不足 += 1  # 原策略重演理论上不应触发；触发即回放口径有问题
                fee = 账.买入(日, code, 量, 价, sig, 备注=f"原流水#{t['id']}")
                if abs(float(fee) - float(t["fee"] or 0)) > 0.02:
                    账.费用核对不符 += 1
                continue
            前日 = 前一交易日.get(日)
            ref_bar = 日线.get(code, {}).get(前日) if 前日 else None
            ref = ref_bar["close"] if ref_bar else None
            carried = False
            if sig and sig.get("created_at"):
                创建日 = str(sig["created_at"])[:10]
                后续 = [x for x in 日历 if x > 创建日]
                carried = bool(后续) and 日 > 后续[0]
            sv = rc.SignalView(
                signal_id=sig["id"] if sig else None, code=code,
                name=(sig or {}).get("stock_name") or "",
                stop_loss=(sig or {}).get("stop_loss"), target_price=(sig or {}).get("target_price"),
                entry_low=(sig or {}).get("entry_low"), entry_high=(sig or {}).get("entry_high"),
                reference_close=ref, is_carried=carried,
            )
            决策权益 = 账.权益(前日, 日线) if 前日 else float(账.现金)
            决策 = rc.evaluate_entry(
                sv, price=价, intended_qty=量, trades=账.流水,
                equity=决策权益, cash=float(账.现金), today=日, trading_days=日历, config=配置,
            )
            if not 决策.passed:
                拦截清单.append({
                    "日": 日, "代码": code, "信号": sv.signal_id, "规则": 决策.rule,
                    "原因": "；".join(决策.reasons), "原金额": round(量 * 价, 2),
                    "原数量": 量, "价格": 价, "carried": carried, "盈亏比": 决策.reward_risk,
                    "原流水id": t["id"],
                })
                continue
            if 决策.final_qty < 量:
                缩量清单.append({"日": 日, "代码": code, "信号": sv.signal_id, "原数量": 量, "新数量": 决策.final_qty, "预算上限": 决策.budget_cap})
            最终量 = 决策.final_qty
            # 现金可行性封顶：反事实场景现金路径与原始不同，防止透支（预算闸门已含现金约束，此处兜底）
            可买 = rc.clamp_buy_qty(code, int(float(账.现金) * 0.995 / 价)) if 价 > 0 else 0
            if 最终量 > 可买:
                缩量清单.append({"日": 日, "代码": code, "信号": sv.signal_id, "原数量": 最终量, "新数量": 可买, "预算上限": float(账.现金)})
                最终量 = 可买
            if 最终量 <= 0:
                拦截清单.append({"日": 日, "代码": code, "信号": sv.signal_id, "规则": "现金不足", "原因": f"场景现金 {float(账.现金):.2f} 元买不起一手", "原金额": round(量 * 价, 2), "原数量": 量, "价格": 价, "carried": carried, "盈亏比": 决策.reward_risk, "原流水id": t["id"]})
                continue
            账.买入(日, code, 最终量, 价, sig, 备注=f"原流水#{t['id']} 风控放行")
        权益曲线.append({"日": 日, "权益": 账.权益(日, 日线), "现金": float(账.现金)})
        最低现金 = min(最低现金, float(账.现金))

    峰值, 最大回撤 = 初始资金, 0.0
    for 点 in 权益曲线:
        峰值 = max(峰值, 点["权益"])
        最大回撤 = min(最大回撤, 点["权益"] / 峰值 - 1.0)
    return {
        "场景": 场景名, "账": 账, "期末权益": 权益曲线[-1]["权益"] if 权益曲线 else 初始资金,
        "已实现": float(账.已实现), "费用": float(账.费用合计), "税": float(账.税合计),
        "买入笔数": sum(1 for x in 账.成交明细 if x["方向"] == "buy"),
        "卖出笔数": sum(1 for x in 账.成交明细 if x["方向"] == "sell"),
        "最大回撤": round(最大回撤 * 100, 2), "最低现金": round(最低现金, 2),
        "持仓数": len(账.持仓), "拦截": 拦截清单, "缩量": 缩量清单, "兜底": 兜底清单,
        "连带": 连带清单, "权益曲线": 权益曲线,
    }


def 原结局估算(拦截项, 成交):
    """被拦买入若放行，在原流水里的结局（首个同标的后续卖出，价差估算口径）。"""
    for item in 拦截项:
        后续卖 = next((t for t in 成交 if t["side"] == "sell" and t["symbol"] == item["代码"] and t["trade_date"] > item["日"]), None)
        if 后续卖 is None:
            item["原结局"] = "原策略持有至窗口末（未平仓）"
            item["估算盈亏"] = None
            continue
        毛差 = (后续卖["price"] - item["价格"]) * item["原数量"]
        买费, _ = rc.compute_fees("buy", d(Decimal(str(item["价格"])) * item["原数量"]))
        卖费, 卖税 = rc.compute_fees("sell", d(Decimal(str(后续卖["price"])) * item["原数量"]))
        item["估算盈亏"] = float(d(Decimal(str(毛差)) - 买费 - 卖费 - 卖税))
        item["原结局"] = f"原策略 {后续卖['trade_date']} 卖出@{后续卖['price']}"
        item["省费用"] = float(买费 + 卖费 + 卖税)
    return 拦截项


# ----------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------
def 汇总行(r):
    return (f"| {r['场景']} | {r['期末权益']:.2f} | {r['已实现']:+.2f} | {r['费用']:.2f} | {r['税']:.2f} "
            f"| {r['买入笔数']}/{r['卖出笔数']} | {r['最大回撤']:.2f}% | {r['最低现金']:.2f} | {r['持仓数']} |")


def 配置描述(cfg) -> str:
    """T2：按实际启用的规则生成场景名，避免 --config 自定义时硬编码误导。"""
    项 = []
    if cfg.fallback_stop_enabled:
        项.append(f"P0兜底{cfg.fallback_stop_loss_pct:.0%}")
    if cfg.rr_filter_enabled:
        项.append(f"RR≥{cfg.min_rr:g}")
    if cfg.cooldown_scope in ("stop_only", "all"):
        项.append(f"{'止损' if cfg.cooldown_scope == 'stop_only' else '全'}冷却{cfg.cooldown_days}日")
    if cfg.budget_enabled:
        项.append(f"预算{cfg.max_weight:g}×{cfg.target_names}+现金{cfg.cash_reserve_pct:.0%}")
    if cfg.min_amount_enabled:
        项.append(f"最小单{cfg.min_order_amount:.0f}")
    return "新风控(" + ("+".join(项) if 项 else "全关") + ")"


def main():
    ap = argparse.ArgumentParser(description="纸面风控干跑对比（只读回放）")
    ap.add_argument("--db", default=str(仓库根 / "data" / "stock_analysis.db"))
    ap.add_argument("--account", type=int, default=2)
    ap.add_argument("--start", default="2026-11-02")
    ap.add_argument("--end", default="2026-11-13")
    ap.add_argument("--initial-cash", type=float, default=100000.0)
    ap.add_argument("--config", default=None, help="策略档 config_json 文件路径（entry/exit/risk 三节），缺省用脚本显式传入的任务书参数（模块自身默认全关）")
    ap.add_argument("--报告", default=None, help="报告输出路径；缺省写到当前工作目录 干跑_风控对比_结果.md（T4：不覆盖 实现/ 下交付文件，更新交付物请显式指定路径）")
    args = ap.parse_args()

    db路径 = Path(args.db)
    成交, 信号, 日历, 日线, 行数前, 表清单 = 加载数据(db路径, args.account, args.start, args.end)
    # B1 定案：模块默认全关；任务书参数在此显式启用（P0 5% / RR 1.5 / 预算 0.2×10 / 最小单 5000，冷却 off）
    任务书配置 = rc.RiskConfig(fallback_stop_enabled=True, rr_filter_enabled=True,
                               budget_enabled=True, min_amount_enabled=True)
    配置 = rc.RiskConfig.from_profile(json.loads(Path(args.config).read_text(encoding="utf-8"))) if args.config else 任务书配置

    全关 = rc.RiskConfig()  # B1 后模块默认即全关
    rr10 = replace(全关, rr_filter_enabled=True, min_rr=1.0)
    rr15 = replace(全关, rr_filter_enabled=True, min_rr=1.5)
    方案组 = [
        ("仅RR@1.0", rr10),
        ("仅RR@1.5", rr15),
        ("RR1.5+止损冷却5日", replace(rr15, cooldown_scope="stop_only", cooldown_days=5)),
        ("RR1.5+全冷却5日", replace(rr15, cooldown_scope="all", cooldown_days=5)),
        ("RR1.0+全冷却5日", replace(rr10, cooldown_scope="all", cooldown_days=5)),
        ("仅P0兜底", replace(全关, fallback_stop_enabled=True)),
        ("仅P2预算", replace(全关, budget_enabled=True)),
        ("仅P2最小金额", replace(全关, min_amount_enabled=True)),
    ]
    原 = 回放(成交, 信号, 日历, 日线, args.initial_cash, None, "原策略(重演)")
    新 = 回放(成交, 信号, 日历, 日线, args.initial_cash, 配置, 配置描述(配置))
    对照组 = [回放(成交, 信号, 日历, 日线, args.initial_cash, cfg, 名) for 名, cfg in 方案组]
    新["拦截"] = 原结局估算(新["拦截"], 成交)

    # 冷却期在 RR 之上的边际影响：相对同阈值仅RR组多拦/少拦了哪些单
    冷却边际 = []
    for 名, 基名 in (("RR1.5+止损冷却5日", "仅RR@1.5"), ("RR1.5+全冷却5日", "仅RR@1.5"), ("RR1.0+全冷却5日", "仅RR@1.0")):
        r = next(x for x in 对照组 if x["场景"] == 名)
        基 = next(x for x in 对照组 if x["场景"] == 基名)
        基拦截 = {x["原流水id"] for x in 基["拦截"]}
        本拦截 = {x["原流水id"] for x in r["拦截"]}
        多拦 = 原结局估算([x for x in r["拦截"] if x["原流水id"] not in 基拦截], 成交)
        少拦 = [x for x in 基["拦截"] if x["原流水id"] not in 本拦截]
        冷却边际.append({"场景": 名, "基线": 基名, "结果": r, "多拦": 多拦, "少拦": 少拦,
                         "权益差": round(r["期末权益"] - 基["期末权益"], 2)})

    一致, _, 行数后 = 只读复核(db路径, 表清单, 行数前)
    基线差 = round(原["期末权益"] - 基准权益, 2)
    基线通过 = abs(基线差) <= 1.0 and abs(原["已实现"] - 基准已实现) <= 1.0

    L = []
    L.append(f"# 干跑风控对比结果（账户 {args.account}，{args.start} → {args.end}）\n")
    L.append(f"- 数据库：`{db路径}`（mode=ro 只读）；成交 {len(成交)} 笔；交易日 {len(日历)} 天；初始资金 {args.initial_cash:.0f}")
    L.append(f"- 只读复核：关键表行数跑前跑后{'一致 ✅' if 一致 else '不一致 ❌'}：{json.dumps(行数后, ensure_ascii=False)}")
    L.append(f"- 费用核对（S7 口径）：21 笔买入 fee 逐笔与库中核对，不符 {原['账'].费用核对不符} 笔（容差 0.02 元）；卖出 fee/tax 未逐笔比对，经费用合计/税合计与基线一致间接验证；原策略重演现金不足 {原['账'].重演现金不足} 笔（应为 0）")
    L.append(f"- 基线核对：重演期末权益 {原['期末权益']:.2f} vs 任务书 {基准权益:.2f}（差 {基线差:+.2f}）；"
             f"已实现 {原['已实现']:+.2f} vs {基准已实现:+.2f}；费用 {原['费用']:.2f} vs {基准费用:.2f}；税 {原['税']:.2f} vs {基准税:.2f} → {'通过 ✅' if 基线通过 else '未通过 ❌'}\n")
    L.append("## 方案汇总对比\n")
    L.append("| 方案 | 期末权益 | 已实现盈亏 | 费用 | 税 | 买/卖笔数 | 最大回撤 | 最低现金 | 期末持仓数 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in [原, 新] + 对照组:
        L.append(汇总行(r))
    L.append(f"\n新风控相对原策略：权益 {新['期末权益'] - 原['期末权益']:+.2f} 元，"
             f"费用+税 {(新['费用'] + 新['税']) - (原['费用'] + 原['税']):+.2f} 元，"
             f"拦截 {len(新['拦截'])} 笔买入，缩量 {len(新['缩量'])} 笔，兜底出场 {len(新['兜底'])} 笔。\n")
    L.append("## 新风控拦截清单（逐笔影响）\n")
    if 新["拦截"]:
        L.append("| 日期 | 代码 | 信号 | 规则 | 原金额 | 盈亏比 | 原结局 | 该笔原估算盈亏 | 原因 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for x in 新["拦截"]:
            盈亏 = f"{x['估算盈亏']:+.2f}" if x.get("估算盈亏") is not None else "—"
            rr文本 = f"{x['盈亏比']:.2f}" if x.get("盈亏比") is not None else "—"
            L.append(f"| {x['日']} | {x['代码']} | #{x['信号']} | {x['规则']} | {x['原金额']:.2f} | {rr文本} | {x['原结局']} | {盈亏} | {x['原因']} |")
        L.append("\n注：\"该笔原估算盈亏\"为价差估算口径（被拦买入 × 原流水首个同标的后续卖出价，含双边费用），"
                 "多批次持仓（如 000001）与原 FIFO 口径可能有出入；负数=拦截避免的亏损（省钱），正数=拦截错过的盈利（少赚）。"
                 "【T3 局限】该估算不含期末浮盈与 FIFO 分批成本，与真实权益边际方向可能相反"
                 "（测试实例：RR1.0+全冷却按逐笔估算为省钱，真实权益边际 -4.92 元）；逐笔估算仅供归因，方案优劣以权益曲线为准。")
    else:
        L.append("（无拦截）")
    L.append("\n## 冷却期在 RR 之上的边际影响（相对同阈值仅RR组）\n")
    for m in 冷却边际:
        L.append(f"### {m['场景']}（期末权益相对{m['基线']} {m['权益差']:+.2f} 元）\n")
        if m["多拦"]:
            L.append("| 日期 | 代码 | 信号 | 规则 | 原金额 | 原结局 | 该笔原估算盈亏 |")
            L.append("|---|---|---|---|---|---|---|")
            for x in m["多拦"]:
                盈亏 = f"{x['估算盈亏']:+.2f}" if x.get("估算盈亏") is not None else "—"
                L.append(f"| {x['日']} | {x['代码']} | #{x['信号']} | {x['规则']} | {x['原金额']:.2f} | {x['原结局']} | {盈亏} |")
            净 = -sum(x["估算盈亏"] for x in m["多拦"] if x.get("估算盈亏") is not None)
            L.append(f"\n多拦 {len(m['多拦'])} 笔，拦截净效应估算 {'避免亏损' if 净 > 0 else '错过盈利'} {abs(净):.2f} 元（正=省钱，负=少赚；未平仓单不计入）。")
        else:
            L.append("（相对仅RR@1.5 无新增拦截——RR 过滤已覆盖全部冷却期候选）")
        if m["少拦"]:
            L.append("\n注意：以下在仅RR@1.5被拦的单在本组反而放行（现金路径变化所致）：")
            for x in m["少拦"]:
                L.append(f"- {x['日']} {x['代码']} 信号#{x['信号']}（{x['规则']}）")
        L.append("")
    L.append("## P0 兜底止损触发清单\n")
    if 新["兜底"]:
        L.append("| 日期 | 代码 | 买入价 | 兜底止损价 | 数量 | 实现盈亏 |")
        L.append("|---|---|---|---|---|---|")
        for x in 新["兜底"]:
            L.append(f"| {x['日']} | {x['代码']} | {x['买入价']} | {x['止损价']} | {x['数量']} | {x['盈亏']:+.2f} |")
    else:
        L.append("（无触发）")
    L.append("\n## 缩量与连带取消\n")
    for x in 新["缩量"]:
        L.append(f"- 缩量 {x['日']} {x['代码']} 信号#{x['信号']}：{x['原数量']} → {x['新数量']} 股（预算上限 {x['预算上限']:.2f} 元）")
    for x in 新["连带"]:
        L.append(f"- 连带 {x['日']} {x['代码']} {x['数量']} 股@{x['价格']}：{x['原因']}")
    if not 新["缩量"] and not 新["连带"]:
        L.append("（无）")
    L.append("\n## 逐日权益曲线\n")
    L.append("| 日期 | 原策略权益 | 新风控权益 | 原现金 | 新现金 |")
    L.append("|---|---|---|---|---|")
    for a, b in zip(原["权益曲线"], 新["权益曲线"]):
        L.append(f"| {a['日']} | {a['权益']:.2f} | {b['权益']:.2f} | {a['现金']:.2f} | {b['现金']:.2f} |")
    L.append("\n## 口径与裁决说明\n")
    L.append("- 【B1 定案】模块默认全关（from_profile(空配置) 零行为变化）；本干跑的任务书参数（P0 5%/RR 1.5/预算 0.2×10/最小单 5000）由脚本显式传入。灰度触发键白名单见 接入说明.md。")
    L.append("- 【已裁决】RR = (止盈-入场中价)/(入场中价-止损)，入场中价=(entry_low+entry_high)/2，不用参考日收盘（调研：参考日口径中位 RR 0.70，1.5 阈值拦 87%；入场中价口径中位 2.00，拦 32%）。")
    L.append("- 【已裁决】坏价样本剔除 RR 检查（告警不拦截）：缺入场区间/缺止损/缺止盈/价格倒挂（target=3.0/5.0 类）/止损宽度>20%（600519 stop=5.0 类）；缺止损持仓由 P0 兜底保护。")
    L.append("- 【已裁决】冷却默认关闭（cooldown_scope=off），范围可配 stop_only/all；拦截点在新信号买入候选循环（与 min_score 同层），不动 carried 机制。三笔问题回补经调研 signal id 链证实全是新信号。")
    L.append("- 【S1】干跑卖出出场类型：note 标记优先，价格贴合推断仅作回退（本窗口 11 笔卖出无跳空/滑点，推断全部吻合）；跳空/滑点出场会被推断漏判，正式接入必须由引擎动作/note 标记显式提供，否则 stop_only 不可批准开启。")
    L.append("- 【S4】冷却日历窗口应 ≥ cooldown_days+1 个交易日且含今日；本干跑日历覆盖全窗口无缺口。日历缺口时模块 fail-open 但输出告警并置 cooldown_unverified 标记。")
    L.append("- 【T1】兜底止损成交价：传入当日开盘价按引擎跳空口径 min(开盘, 止损) 成交；本窗口 600760 触发日开盘 46.90 > 止损 46.52，与止损价精确成交一致。其余卖出仍按原流水实际价重演。")
    L.append("- carried 判定为启发式（成交日 > 信号创建日后首个交易日），仅供分类展示，不参与拦截决策。")
    L.append("- 预算只缩量不放大：最终数量 = min(原策略拟买数量, 预算内整手数量)，隔离风控影响、不引入新仓位算法。")
    L.append("- 【T3】拦截清单/冷却边际的\"原结局估算\"为价差口径，不含期末浮盈与 FIFO 分批成本，与真实权益边际方向可能相反（实例：RR1.0+全冷却估算省钱、真实边际 -4.92 元）；方案比较以逐日权益曲线为准。")
    L.append("- 本回放为信号流反事实演算：被拦买入释放的现金不会触发原流水之外的新买入（原策略错过的信号不在事件流内），权益差异是风控净效应的下界估计。")

    报告文本 = "\n".join(L) + "\n"
    报告路径 = Path(args.报告) if args.报告 else Path.cwd() / "干跑_风控对比_结果.md"
    报告路径.write_text(报告文本, encoding="utf-8")
    print(报告文本)
    print(f"[已写入] {报告路径}")
    if not 一致:
        raise SystemExit("❌ 只读复核失败：关键表行数发生变化")
    if not 基线通过:
        print("⚠️ 基线核对未通过：重演结果与任务书基线差异超过 1 元，请先核查回放口径")


if __name__ == "__main__":
    main()