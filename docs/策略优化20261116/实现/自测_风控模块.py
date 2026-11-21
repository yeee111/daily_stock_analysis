# -*- coding: utf-8 -*-
"""自测_风控模块 —— 纸面风控模块 边界定向测试（纯标准库 unittest，不连库）

运行：python docs/策略优化20261116/实现/自测_风控模块.py
覆盖（2026-11-17 审查返工后口径）：
  B1 默认全关/触发键白名单/既有键不触发/显式开关优先、
  S3 NaN/Infinity 配置防御、S4 冷却日历缺口显式告警、
  S1 exit_kind 显式优先与 note 标记（价格推断仅干跑回退）、
  RR 入场中价口径与坏价五类剔除、cooldown_scope 三态与天数边界、
  兜底止损仅补缺与跳空开盘成交（T1）、预算只缩量不放大、
  最小单金额边界、费用模型与生产常量一致性。
"""

import json
import math
import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import 纸面风控模块 as rc  # noqa: E402

日历 = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
        "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]
# 任务书参数预设（B1 后模块默认全关，任务书口径由调用方显式启用）
默认 = rc.RiskConfig(fallback_stop_enabled=True, rr_filter_enabled=True,
                     budget_enabled=True, min_amount_enabled=True)
全关 = rc.RiskConfig()


def 信号(**kw):
    # 入场区间 9.8~10.2 → 中价 10.0；止损 9.5、止盈 11.0 → RR=2.0
    base = dict(signal_id=1, code="600028", stop_loss=9.5, target_price=11.0,
                entry_low=9.8, entry_high=10.2, reference_close=10.0)
    base.update(kw)
    return rc.SignalView(**base)


def 评估(sig=None, 流水=(), 今日="2026-11-13", 权益=100000.0, 现金=100000.0, 量=1000, 价=10.0, cfg=默认, 交易日历=日历):
    return rc.evaluate_entry(sig or 信号(), price=价, intended_qty=量, trades=list(流水),
                             equity=权益, cash=现金, today=今日, trading_days=交易日历, config=cfg)


class B1_默认全关与触发键(unittest.TestCase):
    def test_模块默认全关(self):
        c = rc.RiskConfig()
        self.assertFalse(c.fallback_stop_enabled or c.rr_filter_enabled or c.budget_enabled or c.min_amount_enabled)
        self.assertEqual(c.cooldown_scope, "off")

    def test_空配置零行为变化(self):
        for cfg in (None, {}, {"entry": {}, "exit": {}, "risk": {}}):
            self.assertEqual(rc.RiskConfig.from_profile(cfg), 全关)

    def test_数值参数默认值保留(self):
        c = rc.RiskConfig.from_profile(None)
        self.assertEqual((c.fallback_stop_loss_pct, c.min_rr, c.cooldown_days, c.min_order_amount), (0.05, 1.5, 5, 5000.0))
        self.assertEqual((c.max_weight, c.target_names, c.cash_reserve_pct, c.rr_max_stop_width), (0.2, 10, 0.05, 0.20))

    def test_触发键逐项启用(self):
        cases = [
            ({"exit": {"fallback_stop_loss_pct": 0.05}}, "fallback_stop_enabled"),
            ({"entry": {"min_rr": 1.5}}, "rr_filter_enabled"),
            ({"entry": {"min_order_amount": 5000}}, "min_amount_enabled"),
            ({"risk": {"cash_reserve_pct": 0.05}}, "budget_enabled"),
        ]
        for cfg, attr in cases:
            parsed = rc.RiskConfig.from_profile(cfg)
            self.assertTrue(getattr(parsed, attr), f"{cfg} 应触发 {attr}")
            # 只触发自己的规则，其余仍关
            others = {"fallback_stop_enabled", "rr_filter_enabled", "min_amount_enabled", "budget_enabled"} - {attr}
            self.assertFalse(any(getattr(parsed, x) for x in others))
        self.assertEqual(rc.RiskConfig.from_profile({"risk": {"stop_cooldown_scope": "all"}}).cooldown_scope, "all")

    def test_既有键不触发任何规则(self):
        c = rc.RiskConfig.from_profile({"entry": {"max_names": 8, "max_weight": 0.15}})
        self.assertEqual(c, replace(全关, target_names=8, max_weight=0.15))  # 仅参数变化，开关全关

    def test_仅参数键不触发(self):
        c = rc.RiskConfig.from_profile({"entry": {"rr_max_stop_width": 0.15}, "risk": {"stop_cooldown_days": 3}})
        self.assertFalse(c.rr_filter_enabled)
        self.assertEqual(c.cooldown_scope, "off")
        self.assertEqual((c.rr_max_stop_width, c.cooldown_days), (0.15, 3))

    def test_显式开关true可单独启用(self):
        c = rc.RiskConfig.from_profile({"risk": {"rr_filter_enabled": True, "budget_enabled": "true"}})
        self.assertTrue(c.rr_filter_enabled and c.budget_enabled)
        self.assertEqual(c.min_rr, 1.5)  # 参数走默认

    def test_显式开关false一票否决(self):
        c = rc.RiskConfig.from_profile({"entry": {"min_rr": 1.5}, "risk": {"rr_filter_enabled": False}})
        self.assertFalse(c.rr_filter_enabled)

    def test_旧键别名兼容(self):
        c = rc.RiskConfig.from_profile({"entry": {"min_reward_risk": 1.2}, "exit": {"fallback_stop_pct": 0.04}})
        self.assertTrue(c.rr_filter_enabled and c.fallback_stop_enabled)
        self.assertEqual((c.min_rr, c.fallback_stop_loss_pct), (1.2, 0.04))

    def test_非法scope回退off(self):
        self.assertEqual(rc.RiskConfig.from_profile({"risk": {"stop_cooldown_scope": "bogus"}}).cooldown_scope, "off")

    def test_cooldown_enabled_false强制关(self):
        c = rc.RiskConfig.from_profile({"risk": {"stop_cooldown_scope": "all", "cooldown_enabled": False}})
        self.assertEqual(c.cooldown_scope, "off")

    def test_数值置零不触发(self):
        c = rc.RiskConfig.from_profile({"entry": {"min_rr": 0, "min_order_amount": 0}, "exit": {"fallback_stop_loss_pct": 0}})
        self.assertFalse(c.rr_filter_enabled or c.min_amount_enabled or c.fallback_stop_enabled)

    def test_全关后一切放行(self):
        流水 = [rc.TradeView("600028", "2026-11-12", "sell", 100, 9.5, "stop_loss")]
        r = 评估(信号(stop_loss=9.9, target_price=10.1), 流水, 今日="2026-11-13", 量=100, 价=10.0, cfg=全关)
        self.assertTrue(r.passed)
        self.assertEqual(r.final_qty, 100)


class S3_非法数值防御(unittest.TestCase):
    def test_NaN天数不崩溃(self):
        cfg = json.loads('{"risk": {"stop_cooldown_scope": "all", "stop_cooldown_days": NaN}}')
        c = rc.RiskConfig.from_profile(cfg)
        self.assertEqual(c.cooldown_days, 5)  # 回退默认
        self.assertEqual(c.cooldown_scope, "all")

    def test_Infinity持仓数不崩溃(self):
        cfg = json.loads('{"entry": {"max_names": Infinity}}')
        c = rc.RiskConfig.from_profile(cfg)
        self.assertEqual(c.target_names, 10)

    def test_NaN现金储备不触发预算(self):
        cfg = json.loads('{"risk": {"cash_reserve_pct": NaN}}')
        c = rc.RiskConfig.from_profile(cfg)
        self.assertFalse(c.budget_enabled)
        self.assertEqual(c.cash_reserve_pct, 0.05)

    def test_溢出字符串视同未配置(self):
        c = rc.RiskConfig.from_profile({"entry": {"min_rr": "1e999"}})
        self.assertFalse(c.rr_filter_enabled)
        self.assertTrue(math.isfinite(c.min_rr))

    def test_非法字符串回退(self):
        c = rc.RiskConfig.from_profile({"entry": {"min_rr": "abc", "max_weight": None}})
        self.assertFalse(c.rr_filter_enabled)
        self.assertEqual(c.max_weight, 0.2)


class S4_冷却日历缺口告警(unittest.TestCase):
    止损流水 = [rc.TradeView("600028", "2026-11-05", "sell", 100, 9.5, "stop_loss")]
    止损cfg = replace(默认, cooldown_scope="stop_only")

    def test_日历为空_failopen并告警(self):
        w = []
        self.assertEqual(rc.cooldown_remaining("600028", "2026-11-06", self.止损流水, [], self.止损cfg, warnings=w), 0)
        self.assertTrue(any("日历为空" in x for x in w))

    def test_缺今日_failopen并告警(self):
        w = []
        self.assertEqual(rc.cooldown_remaining("600028", "2026-11-06", self.止损流水, ["2026-11-02", "2026-11-03"], self.止损cfg, warnings=w), 0)
        self.assertTrue(any("不在交易日历" in x for x in w))

    def test_出场日不在日历_该笔不计数并告警(self):
        w = []
        短日历 = ["2026-11-06", "2026-11-09"]
        self.assertEqual(rc.cooldown_remaining("600028", "2026-11-06", self.止损流水, 短日历, self.止损cfg, warnings=w), 0)
        self.assertTrue(any("出场日 2026-11-05" in x for x in w))

    def test_闸门透传告警与标记(self):
        r = 评估(流水=self.止损流水, 今日="2026-11-06", cfg=self.止损cfg, 交易日历=["2026-11-06", "2026-11-09"])
        self.assertTrue(r.passed)  # fail-open
        self.assertTrue(r.cooldown_unverified)
        self.assertTrue(any("冷却告警" in x for x in r.reasons))

    def test_正常日历无告警标记(self):
        r = 评估(流水=self.止损流水, 今日="2026-11-13", cfg=self.止损cfg)
        self.assertFalse(r.cooldown_unverified)


class S1_出场类型解析优先级(unittest.TestCase):
    def test_显式动作优先于价格(self):
        # 成交价贴合止盈，但引擎动作是止损（跳空/滑点场景）→ 以显式为准
        self.assertEqual(rc.resolve_exit_kind(explicit="stop_loss", sell_price=35.22, stop_loss=33.2,
                                              target_price=35.22, allow_price_inference=True), "stop_loss")

    def test_note标记解析(self):
        self.assertEqual(rc.resolve_exit_kind(note="paper trading: signal #142 [stop_loss]"), "stop_loss")
        self.assertEqual(rc.resolve_exit_kind(note="paper trading: signal #54 [take_profit]"), "take_profit")
        self.assertEqual(rc.resolve_exit_kind(note="止损出场 signal #95"), "stop_loss")

    def test_note标记优先于价格推断(self):
        self.assertEqual(rc.resolve_exit_kind(note="[stop_loss]", sell_price=50.0, stop_loss=47.3,
                                              target_price=50.0, allow_price_inference=True), "stop_loss")

    def test_跳空止损价格推断漏判_必须显式(self):
        # 开盘 47.0 跳空穿越止损 47.3 → 引擎按开盘成交，推断返回 None（S1 局限实证）
        self.assertIsNone(rc.resolve_exit_kind(sell_price=47.0, stop_loss=47.3, target_price=50.0,
                                               allow_price_inference=True))
        self.assertEqual(rc.resolve_exit_kind(explicit="stop_loss", sell_price=47.0), "stop_loss")

    def test_默认不允许价格推断(self):
        self.assertIsNone(rc.resolve_exit_kind(sell_price=5.52, stop_loss=5.52, target_price=5.95))
        self.assertEqual(rc.resolve_exit_kind(sell_price=5.52, stop_loss=5.52, target_price=5.95,
                                              allow_price_inference=True), "stop_loss")


class 盈亏比_入场中价口径(unittest.TestCase):
    def test_用入场中价不用参考收盘(self):
        r = 评估(信号(reference_close=10.9))
        self.assertTrue(r.passed)
        self.assertEqual(r.reward_risk, 2.0)

    def test_等于阈值放行(self):
        self.assertTrue(评估(信号(stop_loss=9.0, target_price=11.5)).passed)

    def test_低于阈值拦截(self):
        r = 评估(信号(stop_loss=9.0, target_price=11.49))
        self.assertFalse(r.passed)
        self.assertEqual(r.rule, "盈亏比")

    def test_阈值可配(self):
        sig = 信号(stop_loss=9.0, target_price=11.2)  # RR=1.2
        self.assertFalse(评估(sig).passed)
        self.assertTrue(评估(sig, cfg=replace(默认, min_rr=1.0)).passed)

    def test_入场区间单侧缺失_RR为None不拦截(self):
        r = 评估(信号(entry_high=None))
        self.assertTrue(r.passed)
        self.assertIsNone(r.reward_risk)
        self.assertTrue(r.rr_skipped)

    def test_入场区间倒挂视为缺失(self):
        r = 评估(信号(entry_low=10.2, entry_high=9.8))
        self.assertTrue(r.rr_skipped)
        self.assertIsNone(r.reward_risk)


class 坏价剔除(unittest.TestCase):
    def test_坏目标3元类_倒挂剔除不拦截(self):
        r = 评估(信号(entry_low=11.5, entry_high=11.9, stop_loss=11.3, target_price=3.0))
        self.assertTrue(r.passed)
        self.assertTrue(r.rr_skipped)
        self.assertTrue(any("倒挂" in x for x in r.reasons))

    def test_坏目标5元类(self):
        r = 评估(信号(entry_low=86.0, entry_high=88.0, stop_loss=84.0, target_price=5.0))
        self.assertTrue(r.passed and r.rr_skipped)

    def test_600519坏止损_宽度99pct剔除(self):
        r = 评估(信号(code="600519", entry_low=1320.0, entry_high=1340.0, stop_loss=5.0, target_price=1500.0))
        self.assertTrue(r.passed)
        self.assertTrue(r.rr_skipped)
        self.assertTrue(any("止损宽度" in x for x in r.reasons))

    def test_宽度防护可配关闭(self):
        sig = 信号(code="600519", entry_low=1320.0, entry_high=1340.0, stop_loss=5.0, target_price=1500.0)
        r = 评估(sig, cfg=replace(默认, rr_max_stop_width=0))
        self.assertFalse(r.rr_skipped)
        self.assertFalse(r.passed)

    def test_宽度恰20pct不剔除(self):
        r = 评估(信号(stop_loss=8.0, target_price=13.0))
        self.assertFalse(r.rr_skipped)
        self.assertTrue(r.passed)

    def test_缺止损剔除_走P0兜底(self):
        r = 评估(信号(stop_loss=None))
        self.assertTrue(r.passed and r.rr_skipped)
        self.assertTrue(any("P0 兜底" in x for x in r.reasons))

    def test_缺止盈剔除(self):
        r = 评估(信号(target_price=None))
        self.assertTrue(r.passed and r.rr_skipped)

    def test_止损不低于中价剔除(self):
        r = 评估(信号(stop_loss=10.0))
        self.assertTrue(r.passed and r.rr_skipped)


class 冷却三态与边界(unittest.TestCase):
    止损流水 = [rc.TradeView("600028", "2026-11-05", "sell", 100, 9.5, "stop_loss")]
    止盈流水 = [rc.TradeView("600028", "2026-11-05", "sell", 100, 11.0, "take_profit")]
    止损cfg = replace(默认, cooldown_scope="stop_only")
    全冷cfg = replace(默认, cooldown_scope="all")

    def test_默认off不拦(self):
        self.assertEqual(默认.cooldown_scope, "off")
        self.assertTrue(评估(流水=self.止损流水, 今日="2026-11-06").passed)

    def test_stop_only拦止损后回补(self):
        r = 评估(流水=self.止损流水, 今日="2026-11-06", cfg=self.止损cfg)
        self.assertFalse(r.passed)
        self.assertEqual(r.rule, "出场冷却")

    def test_stop_only不拦止盈后回补(self):
        self.assertTrue(评估(流水=self.止盈流水, 今日="2026-11-06", cfg=self.止损cfg).passed)

    def test_all拦止盈后回补(self):
        self.assertFalse(评估(流水=self.止盈流水, 今日="2026-11-06", cfg=self.全冷cfg).passed)

    def test_all拦未知类型卖出(self):
        流水 = [rc.TradeView("600028", "2026-11-05", "sell", 100, 10.3, None)]
        self.assertFalse(评估(流水=流水, 今日="2026-11-06", cfg=self.全冷cfg).passed)
        self.assertTrue(评估(流水=流水, 今日="2026-11-06", cfg=self.止损cfg).passed)

    def test_出场当日回补拦截_剩余数含今日(self):
        # T3 口径：elapsed=0 → 剩 days+1=6（含今日），T+1..T+5 共 5 日 + 当日
        self.assertEqual(rc.cooldown_remaining("600028", "2026-11-05", self.止损流水, 日历, self.止损cfg), 6)

    def test_第5个交易日仍拦(self):
        self.assertEqual(rc.cooldown_remaining("600028", "2026-11-12", self.止损流水, 日历, self.止损cfg), 1)

    def test_第6个交易日放行(self):
        self.assertEqual(rc.cooldown_remaining("600028", "2026-11-13", self.止损流水, 日历, self.止损cfg), 0)
        self.assertTrue(评估(流水=self.止损流水, 今日="2026-11-13", cfg=self.止损cfg).passed)

    def test_天数可配(self):
        cfg = replace(self.止损cfg, cooldown_days=1)
        self.assertFalse(评估(流水=self.止损流水, 今日="2026-11-06", cfg=cfg).passed)
        self.assertTrue(评估(流水=self.止损流水, 今日="2026-11-09", cfg=cfg).passed)

    def test_跨标的互不影响(self):
        self.assertEqual(rc.cooldown_remaining("600276", "2026-11-06", self.止损流水, 日历, self.全冷cfg), 0)


class 兜底止损(unittest.TestCase):
    def test_裸仓补缺(self):
        pos = rc.PositionView("600760", 200, 48.97, None, "")
        self.assertEqual(rc.fallback_stop(pos, 默认), (46.52, "fallback"))

    def test_已有止损不覆盖(self):
        pos = rc.PositionView("600028", 100, 5.81, 5.52, "signal")
        self.assertEqual(rc.fallback_stop(pos, 默认), (5.52, "signal"))
        self.assertIsNone(rc.check_stop_trigger(pos, 5.0, 默认))

    def test_触线判定(self):
        pos = rc.PositionView("600760", 200, 48.97, None, "")
        self.assertEqual(rc.check_stop_trigger(pos, 46.5, 默认), 46.52)
        self.assertIsNone(rc.check_stop_trigger(pos, 46.53, 默认))
        self.assertIsNone(rc.check_stop_trigger(pos, None, 默认))

    def test_跳空开盘按开盘成交_T1(self):
        pos = rc.PositionView("600760", 200, 48.97, None, "")
        # 开盘 46.0 已经低于兜底止损 46.52 → 引擎口径按开盘成交
        self.assertEqual(rc.check_stop_trigger(pos, 45.8, 默认, day_open=46.0), 46.0)
        # 开盘 46.9 高于止损、盘中触线 → 按止损价成交
        self.assertEqual(rc.check_stop_trigger(pos, 46.5, 默认, day_open=46.9), 46.52)

    def test_比例可配(self):
        pos = rc.PositionView("600760", 200, 48.97, None, "")
        stop, _ = rc.fallback_stop(pos, replace(默认, fallback_stop_loss_pct=0.08))
        self.assertEqual(stop, 45.05)

    def test_关闭后裸仓保持裸仓(self):
        pos = rc.PositionView("600760", 200, 48.97, None, "")
        self.assertEqual(rc.fallback_stop(pos, 全关), (None, ""))


class 预算与最小金额(unittest.TestCase):
    def test_预算取两者较小(self):
        r = 评估(量=3000, 价=10.0)
        self.assertTrue(r.passed)
        self.assertEqual(r.final_qty, 900)  # 10000×0.995/10=995 → 整手 900

    def test_只缩量不放大(self):
        r = 评估(量=100, 价=10.0, cfg=replace(默认, min_amount_enabled=False))
        self.assertTrue(r.passed)
        self.assertEqual(r.final_qty, 100)
        r2 = 评估(量=100, 价=10.0)
        self.assertFalse(r2.passed)
        self.assertEqual(r2.final_qty, 0)

    def test_常备现金约束(self):
        r = 评估(量=1000, 价=10.0, 现金=10500.0)
        self.assertEqual(r.final_qty, 500)

    def test_最小金额边界(self):
        self.assertTrue(评估(量=500, 价=10.0).passed)
        self.assertFalse(评估(量=400, 价=10.0).passed)
        self.assertTrue(评估(量=400, 价=10.0, cfg=replace(默认, min_amount_enabled=False)).passed)

    def test_买不起一手拦截(self):
        r = 评估(信号(code="300760", entry_low=177.5, entry_high=179.5, stop_loss=175.3, target_price=186.0),
                 量=100, 价=180.0)
        self.assertFalse(r.passed)
        self.assertEqual(r.rule, "现金预算")


class 费用与整手(unittest.TestCase):
    def test_最低佣金(self):
        fee, tax = rc.compute_fees("buy", Decimal("581.00"))
        self.assertEqual((fee, tax), (Decimal("5.01"), Decimal("0.00")))  # 与库中 600028 首笔一致

    def test_卖出印花税(self):
        fee, tax = rc.compute_fees("sell", Decimal("18900.00"))
        self.assertEqual((fee, tax), (Decimal("5.19"), Decimal("9.45")))  # 与库中 600887 卖出一致

    def test_整手规则(self):
        self.assertEqual(rc.clamp_buy_qty("600028", 1750), 1700)
        self.assertEqual(rc.clamp_buy_qty("300760", 99), 0)
        self.assertEqual(rc.clamp_buy_qty("688001", 250), 250)
        self.assertEqual(rc.clamp_buy_qty("688001", 199), 0)

    def test_出场类型推断_干跑回退(self):
        self.assertEqual(rc.infer_exit_kind(5.52, 5.52, 5.95), "stop_loss")
        self.assertEqual(rc.infer_exit_kind(35.22, 33.2, 35.22), "take_profit")
        self.assertIsNone(rc.infer_exit_kind(34.0, 33.2, 35.22))


if __name__ == "__main__":
    unittest.main(verbosity=2)