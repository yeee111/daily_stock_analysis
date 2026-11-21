# -*- coding: utf-8 -*-
"""12 日回放：现成风控开关 + 三道新闸门（买区/追高、实际盈亏比、坏目标）。

只读回放账户 2 的实际成交流水（2026-11-02 → 2026-11-17），复用
`实现/干跑_风控对比.py` 的加载与回放引擎（同一份 paper_risk_control 单一真源）。

新增闸门只在**本脚本内**以 pre-gate 形式实现，`src/services/paper_risk_control.py`
与 `实现/` 下的交付脚本均未改动。三道闸门对应的问题：

  追高闸门   成交价 > entry_high×(1+容忍) 或 < entry_low×(1-3%) → 拦截
             （分析的本意是"回踩买区再买"，引擎按开盘价买会买在压力位下方）
  实际盈亏比 用**实际成交价**重算 RR=(target-price)/(price-stop)；
             并要求 target 距成交价 ≥ 最低空间（默认 1.5%），
             否则一次往返费用就能吃掉全部利润
  坏目标     target 缺失 / target ≤ 成交价 / 缺买区 → 拦截
             （例如 002714 #428 target=10.0、stop=None）

安全：sqlite 以 mode=ro 打开；跑前跑后核对关键表行数；不写任何库表。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

实现目录 = Path(__file__).resolve().parents[1] / "实现"
if str(实现目录) not in sys.path:
    sys.path.insert(0, str(实现目录))

import 干跑_风控对比 as g  # noqa: E402

rc = g.rc

# 闸门参数（与用户 2026-11-17 讨论一致）
追高容忍 = 0.005      # 成交价高于买区上沿 0.5% 以内仍接受
跌破容忍 = 0.03       # 成交价低于买区下沿 3% 以内仍接受
最低空间 = 0.015      # 止盈距成交价至少 1.5%
最低实际RR = 1.5
冷却天数 = 3

闸门开关 = {"买区": False, "止盈空间": False, "实际RR": False, "坏目标": False, "缺买区": False,
            "最低空间": 最低空间, "最低实际RR": 最低实际RR}
原评估 = rc.evaluate_entry


def _拦截(rule: str, reasons):
    return rc.GateDecision(passed=False, rule=rule, reasons=tuple(reasons), final_qty=0)


def 增强评估(signal, **kwargs):
    """pre-gate：三道新闸门全部命中项都记录；未命中则回落到原 evaluate_entry。"""
    价格 = float(kwargs["price"])
    命中 = []

    if 闸门开关["坏目标"]:
        target = signal.target_price
        if not target or target <= 0:
            命中.append(("坏目标", f"{signal.code} 缺止盈价"))
        elif target <= 价格:
            命中.append(("坏目标", f"{signal.code} 止盈 {target} ≤ 成交价 {价格:.3f}（价格倒挂）"))
        if 闸门开关["缺买区"] and (signal.entry_low is None or signal.entry_high is None):
            命中.append(("缺买区", f"{signal.code} 缺入场区间，无法确认是否追高"))

    if 闸门开关["买区"] and signal.entry_low and signal.entry_high:
        low, high = float(signal.entry_low), float(signal.entry_high)
        if 价格 > high * (1 + 追高容忍):
            命中.append(("追高", f"{signal.code} 成交价 {价格:.3f} > 买区上沿 {high:.3f}（超出 {(价格 / high - 1):.1%}）"))
        elif 价格 < low * (1 - 跌破容忍):
            命中.append(("跌破买区", f"{signal.code} 成交价 {价格:.3f} < 买区下沿 {low:.3f}"))

    if signal.target_price and signal.target_price > 0:
        space = (float(signal.target_price) - 价格) / 价格
        if 闸门开关["止盈空间"] and space < 闸门开关["最低空间"]:
            命中.append(("空间不足", f"{signal.code} 止盈空间 {space:.2%} < {闸门开关['最低空间']:.1%}"))
        stop = signal.stop_loss
        if 闸门开关["实际RR"] and stop and 0 < float(stop) < 价格:
            rr = (float(signal.target_price) - 价格) / (价格 - float(stop))
            if rr < 闸门开关["最低实际RR"]:
                命中.append(("实际盈亏比", f"{signal.code} 实际 RR {rr:.2f} < {闸门开关['最低实际RR']:g}（成交价 {价格:.3f}，止损 {stop}，止盈 {signal.target_price}）"))

    if 命中:
        return _拦截(命中[0][0], [文本 for _, 文本 in 命中])
    return 原评估(signal, **kwargs)


rc.evaluate_entry = 增强评估


def 跑(成交, 信号, 日历, 日线, 初始资金, 配置, 名, *, 买区=False, 止盈空间=False,
       实际RR=False, 坏目标=False, 缺买区=False, 最低空间_=最低空间, 最低实际RR_=最低实际RR):
    闸门开关.update({"买区": 买区, "止盈空间": 止盈空间, "实际RR": 实际RR, "坏目标": 坏目标,
                    "缺买区": 缺买区, "最低空间": 最低空间_, "最低实际RR": 最低实际RR_})
    try:
        return g.回放(成交, 信号, 日历, 日线, 初始资金, 配置, 名)
    finally:
        闸门开关.update({"买区": False, "止盈空间": False, "实际RR": False, "坏目标": False,
                        "缺买区": False})


def 汇总行(r):
    return (f"| {r['场景']} | {r['期末权益']:.2f} | {r['已实现']:+.2f} | {r['费用']:.2f} "
            f"| {r['税']:.2f} | {r['买入笔数']}/{r['卖出笔数']} | {r['最大回撤']:.2f}% "
            f"| {len(r['拦截'])} |")


def 拦截表(行, 成交):
    if not 行:
        return "（无拦截）"
    出 = ["| 日期 | 代码 | 信号 | 规则 | 原金额 | 原结局 | 该笔原估算盈亏 | 原因 |",
          "|---|---|---|---|---|---|---|---|"]
    for x in 行:
        盈亏 = f"{x['估算盈亏']:+.2f}" if x.get("估算盈亏") is not None else "—"
        出.append(f"| {x['日']} | {x['代码']} | #{x['信号']} | {x['规则']} | {x['原金额']:.2f} "
                  f"| {x['原结局']} | {盈亏} | {x['原因']} |")
    return "\n".join(出)


def main():
    ap = argparse.ArgumentParser(description="12 日追高闸门干跑回放（只读）")
    仓库根 = 实现目录.parents[1].parent
    ap.add_argument("--db", default=str(仓库根 / "data" / "stock_analysis.db"))
    ap.add_argument("--account", type=int, default=2)
    ap.add_argument("--start", default="2026-11-02")
    ap.add_argument("--end", default="2026-11-17")
    ap.add_argument("--initial-cash", type=float, default=100000.0)
    ap.add_argument("--报告", default=None)
    args = ap.parse_args()

    db路径 = Path(args.db)
    成交, 信号, 日历, 日线, 行数前, 表清单 = g.加载数据(db路径, args.account, args.start, args.end)

    全关 = rc.RiskConfig()
    现成开关 = replace(全关, rr_filter_enabled=True, min_rr=1.5,
                       cooldown_scope="all", cooldown_days=冷却天数,
                       min_amount_enabled=True, min_order_amount=5000.0)

    仅冷却 = replace(全关, cooldown_scope="all", cooldown_days=冷却天数)

    原 = 跑(成交, 信号, 日历, 日线, args.initial_cash, None, "原策略(重演)")
    s1 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 现成开关, "S1 现成开关(RR1.5+全冷却3日+最小单5000)")
    s2 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 现成开关, "S2 S1+三闸门(买区+空间+实际RR)",
            买区=True, 止盈空间=True, 实际RR=True, 坏目标=True)
    s3 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 全关, "S3 仅三闸门",
            买区=True, 止盈空间=True, 实际RR=True, 坏目标=True)
    v1 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 全关, "V1 仅坏目标+止盈空间1.5%",
            止盈空间=True, 坏目标=True)
    v2 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 全关, "V2 仅坏目标+止盈空间1.0%",
            止盈空间=True, 坏目标=True, 最低空间_=0.01)
    v3 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 仅冷却, "V3 仅全冷却3日")
    v4 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 仅冷却, "V4 冷却3日+坏目标+空间1.5%(含缺买区)",
            止盈空间=True, 坏目标=True, 缺买区=True)
    v5 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 全关, "V5 仅坏目标+止盈空间1.5%(不拦缺买区)",
            止盈空间=True, 坏目标=True)
    v6 = 跑(成交, 信号, 日历, 日线, args.initial_cash, 仅冷却, "V6 冷却3日+坏目标+空间1.5%(不拦缺买区)",
            止盈空间=True, 坏目标=True)

    全部 = (s1, s2, s3, v1, v2, v3, v4, v5, v6)
    for r in 全部:
        r["拦截"] = g.原结局估算(r["拦截"], 成交)

    s1ids = {x["原流水id"] for x in s1["拦截"]}
    边际 = g.原结局估算([x for x in s2["拦截"] if x["原流水id"] not in s1ids], 成交)
    边际净 = -sum(x["估算盈亏"] for x in 边际 if x.get("估算盈亏") is not None)

    一致, _, 行数后 = g.只读复核(db路径, 表清单, 行数前)

    原1113 = next((点 for 点 in 原["权益曲线"] if 点["日"] == "2026-11-13"), None)
    原1117 = 原["权益曲线"][-1] if 原["权益曲线"] else None
    基线1113 = round(原1113["权益"] - g.基准权益, 2) if 原1113 else None
    独立1117 = 102865.52  # 本机独立核算：成交流水现金 + 11-17 收盘市值

    L = []
    L.append(f"# 12 日干跑回放：追高闸门 + 实际盈亏比（账户 {args.account}，{args.start} → {args.end}）\n")
    L.append(f"- 数据库：`{db路径}`（mode=ro 只读）；成交 {len(成交)} 笔；交易日 {len(日历)} 天；初始资金 {args.initial_cash:.0f}")
    L.append(f"- 只读复核：关键表行数跑前跑后{'一致 ✅' if 一致 else '不一致 ❌'}")
    L.append(f"- 校准①（11-13）：重演权益 {原1113['权益']:.2f} vs 任务书基线 {g.基准权益:.2f}（差 {基线1113:+.2f}）→ {'通过 ✅' if abs(基线1113) <= 1.0 else '未通过 ❌'}")
    L.append(f"- 校准②（11-17）：重演权益 {原1117['权益']:.2f} vs 独立核算 {独立1117:.2f}（差 {原1117['权益'] - 独立1117:+.2f}）→ {'通过 ✅' if abs(原1117['权益'] - 独立1117) <= 1.0 else '未通过 ❌'}")
    L.append(f"- 期间：费用 {原['费用']:.2f} + 税 {原['税']:.2f} = {原['费用'] + 原['税']:.2f} 元；已实现 {原['已实现']:+.2f} 元；期末浮盈见权益差\n")

    L.append("## 方案汇总\n")
    L.append("| 方案 | 期末权益 | 已实现盈亏 | 费用 | 税 | 买/卖笔数 | 最大回撤 | 拦截笔数 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in (原, *全部):
        L.append(汇总行(r))
    L.append("")
    for r in 全部:
        L.append(f"- {r['场景']} 相对原策略：权益 {r['期末权益'] - 原['期末权益']:+.2f} 元，"
                 f"费用+税 {(r['费用'] + r['税']) - (原['费用'] + 原['税']):+.2f} 元，"
                 f"买入 {r['买入笔数'] - 原['买入笔数']:+d} 笔")
    L.append("")

    L.append("## S1 现成开关拦截清单\n")
    L.append(拦截表(s1["拦截"], 成交))
    L.append("")
    L.append("## S2 全量三闸门的**边际**拦截（相对 S1）\n")
    L.append(拦截表(边际, 成交))
    L.append(f"\n边际拦截 {len(边际)} 笔；逐笔价差估算净效应：{'避免亏损' if 边际净 > 0 else '错过盈利'} {abs(边际净):.2f} 元"
             "（正=省钱，负=少赚；未平仓单不计入）。权益边际以汇总表为准。\n")
    L.append("## V1 仅坏目标+止盈空间1.5% 拦截清单\n")
    L.append(拦截表(v1["拦截"], 成交))
    L.append("")
    L.append("## V2 仅坏目标+止盈空间1.0% 拦截清单\n")
    L.append(拦截表(v2["拦截"], 成交))
    L.append("")
    L.append("## V3 仅全冷却3日 拦截清单\n")
    L.append(拦截表(v3["拦截"], 成交))
    L.append("")
    L.append("## V4 冷却3日+坏目标+空间1.5% 拦截清单\n")
    L.append(拦截表(v4["拦截"], 成交))
    L.append("")
    L.append("## 逐日权益曲线（原策略 vs V4）\n")
    L.append("| 日期 | 原策略权益 | V4 权益 | 原现金 | V4 现金 |")
    L.append("|---|---|---|---|---|")
    for a, b in zip(原["权益曲线"], v4["权益曲线"]):
        L.append(f"| {a['日']} | {a['权益']:.2f} | {b['权益']:.2f} | {a['现金']:.2f} | {b['现金']:.2f} |")
    L.append("\n## 口径说明\n")
    L.append(f"- 新闸门参数：追高容忍 {追高容忍:.1%}、跌破容忍 {跌破容忍:.0%}、最低止盈空间 {最低空间:.1%}、最低实际 RR {最低实际RR:g}；"
             f"现成开关：RR≥1.5（入场中价口径）、全冷却 {冷却天数} 日、最小单 5000 元。")
    L.append("- S1 的 RR 用信号入场中价，S2 另有实际成交价 RR/止盈空间闸门；两者同时开启时先判实际价。")
    L.append("- 回放为信号流反事实演算：被拦买入释放的现金不会触发原流水之外的新买入，权益差是净效应的下界估计。")
    L.append("- 002714 #428（target=10.0、stop=None）在 11-17 生成、按批次 11-18 才成交，不在本窗口事件流内；"
             "本次回放不含它，但坏目标闸门会在 11-18 直接拦截。")
    L.append("- 本次新闸门仅在本脚本内实现，`src/services/paper_risk_control.py` 与生产引擎均未改动。")

    文本 = "\n".join(L) + "\n"
    路径 = Path(args.报告) if args.报告 else Path(__file__).resolve().parent / "干跑_追高闸门_12日_结果.md"
    路径.write_text(文本, encoding="utf-8")
    print(文本)
    print(f"[已写入] {路径}")
    if not 一致:
        raise SystemExit("❌ 只读复核失败：关键表行数发生变化")


if __name__ == "__main__":
    main()
