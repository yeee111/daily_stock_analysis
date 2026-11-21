# -*- coding: utf-8 -*-
"""边界抽查（测试工程师交付，只读，不改 实现/ 与 src/ 任何文件）。

覆盖：
  A. RR 恰等于阈值 / 略低于阈值（1.0 与 1.5 两档）
  B. 冷却第 5/6 交易日切换、出场当日回补、scope 三态、非法 scope 回退、默认必须显式开启
  C. 坏价五类剔除（倒挂/坏止损宽度/缺入场区间/缺止损/缺止盈）+ 止损宽度恰 20% 边界
  D. 坏价样本是告警不拦截；正常单在同等配置下不被误拦
  E. 生产库真实坏价信号（600760 #165、600519 类）在模块 API 下确实 rr_skipped 且放行
     （干跑层面的剔除证据另见 对照报告.md 对干跑输出的核对）

运行：python 边界抽查.py   （E 部分对 data/stock_analysis.db 以 mode=ro 打开）
"""
import sqlite3
import sys
import unittest
from pathlib import Path

实现目录 = Path(__file__).resolve().parents[1] / "实现"
sys.path.insert(0, str(实现目录))
import 纸面风控模块 as rc  # noqa: E402

仓库根 = Path(__file__).resolve().parents[3]
日历 = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
        "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
        "2026-11-16", "2026-11-17", "2026-11-18"]

仅RR = lambda rr: rc.RiskConfig(fallback_stop_enabled=False, rr_filter_enabled=True, min_rr=rr,
                                cooldown_scope="off", budget_enabled=False, min_amount_enabled=False)
仅冷却 = lambda scope: rc.RiskConfig(fallback_stop_enabled=False, rr_filter_enabled=False,
                                     cooldown_scope=scope, cooldown_days=5,
                                     budget_enabled=False, min_amount_enabled=False)


def 评估(sig, cfg, today="2026-11-13", trades=(), price=10.0, qty=1000):
    return rc.evaluate_entry(sig, price=price, intended_qty=qty, trades=list(trades),
                             equity=100000.0, cash=100000.0, today=today,
                             trading_days=日历, config=cfg)


def 信号(code="600000", stop=9.0, target=12.0, low=9.8, high=10.2, sid=1):
    return rc.SignalView(signal_id=sid, code=code, stop_loss=stop, target_price=target,
                         entry_low=low, entry_high=high)


class A_RR阈值边界(unittest.TestCase):
    def test_RR恰等于1_0放行(self):
        d = 评估(信号(stop=9.0, target=11.0), 仅RR(1.0))  # mid=10, RR=1.0
        self.assertTrue(d.passed, d.reasons)
        self.assertEqual(d.reward_risk, 1.0)

    def test_RR略低于1_0拦截(self):
        d = 评估(信号(stop=9.0, target=10.99), 仅RR(1.0))  # RR=0.99
        self.assertFalse(d.passed)
        self.assertEqual(d.rule, "盈亏比")

    def test_RR恰等于1_5放行(self):
        d = 评估(信号(stop=9.0, target=11.5), 仅RR(1.5))  # RR=1.5
        self.assertTrue(d.passed, d.reasons)
        self.assertEqual(d.reward_risk, 1.5)

    def test_RR略低于1_5拦截(self):
        d = 评估(信号(stop=9.0, target=11.49), 仅RR(1.5))  # RR=1.49
        self.assertFalse(d.passed)


class B_冷却边界(unittest.TestCase):
    止损出场 = rc.TradeView("600000", "2026-11-04", "sell", 100, 9.0, exit_kind="stop_loss")
    止盈出场 = rc.TradeView("600000", "2026-11-04", "sell", 100, 12.0, exit_kind="take_profit")

    def test_出场当日回补拦截(self):
        d = 评估(信号(), 仅冷却("stop_only"), today="2026-11-04", trades=[self.止损出场])
        self.assertFalse(d.passed)
        self.assertEqual(d.rule, "出场冷却")

    def test_第5交易日仍拦截(self):
        # 出场 11-04(idx2)，第5个交易日 = 11-11(idx7)，elapsed=5 ≤ 5 → 拦，剩1天
        d = 评估(信号(), 仅冷却("stop_only"), today="2026-11-11", trades=[self.止损出场])
        self.assertFalse(d.passed)
        self.assertIn("剩 1 个交易日", d.reasons[0])

    def test_第6交易日放行(self):
        # 第6个交易日 = 11-12(idx8)，elapsed=6 > 5 → 放行
        d = 评估(信号(), 仅冷却("stop_only"), today="2026-11-12", trades=[self.止损出场])
        self.assertTrue(d.passed, d.reasons)

    def test_stop_only不拦止盈回补(self):
        d = 评估(信号(), 仅冷却("stop_only"), today="2026-11-05", trades=[self.止盈出场])
        self.assertTrue(d.passed, d.reasons)

    def test_all拦止盈回补(self):
        d = 评估(信号(), 仅冷却("all"), today="2026-11-05", trades=[self.止盈出场])
        self.assertFalse(d.passed)

    def test_all拦未知出场类型(self):
        t = rc.TradeView("600000", "2026-11-04", "sell", 100, 10.5, exit_kind=None)
        d = 评估(信号(), 仅冷却("all"), today="2026-11-05", trades=[t])
        self.assertFalse(d.passed)

    def test_off不拦(self):
        d = 评估(信号(), 仅冷却("off"), today="2026-11-05", trades=[self.止损出场])
        self.assertTrue(d.passed, d.reasons)

    def test_默认配置冷却关闭(self):
        self.assertEqual(rc.RiskConfig().cooldown_scope, "off")
        self.assertEqual(rc.RiskConfig.from_profile({}).cooldown_scope, "off")
        self.assertEqual(rc.RiskConfig.from_profile({"entry": {}, "exit": {}, "risk": {}}).cooldown_scope, "off")

    def test_非法scope回退off(self):
        cfg = rc.RiskConfig.from_profile({"risk": {"stop_cooldown_scope": "随便"}})
        self.assertEqual(cfg.cooldown_scope, "off")

    def test_跨周末按交易日计数(self):
        # 出场 11-06(周五,idx4)，11-09(周一)是第1个交易日而非第3天
        t = rc.TradeView("600000", "2026-11-06", "sell", 100, 9.0, exit_kind="stop_loss")
        d = 评估(信号(), 仅冷却("stop_only"), today="2026-11-13", trades=[t])
        # 11-13 idx9, elapsed=5 → 仍拦（若按自然日早就该放行）
        self.assertFalse(d.passed)
        d2 = 评估(信号(), 仅冷却("stop_only"), today="2026-11-16", trades=[t])
        self.assertTrue(d2.passed, d2.reasons)


class C_坏价与宽度边界(unittest.TestCase):
    def test_价格倒挂剔除不拦截(self):
        d = 评估(信号(stop=5.0, target=3.0), 仅RR(1.5))
        self.assertTrue(d.passed, d.reasons)
        self.assertTrue(d.rr_skipped)
        self.assertIsNone(d.reward_risk)

    def test_坏止损宽度剔除不拦截(self):
        # 600519 类：mid=1410，stop=5.0，宽度 99.6% > 20%
        d = 评估(信号(code="600519", stop=5.0, target=1500.0, low=1400.0, high=1420.0),
                 仅RR(1.5), price=1410.0, qty=100)
        self.assertTrue(d.passed, d.reasons)
        self.assertTrue(d.rr_skipped)

    def test_缺入场区间剔除(self):
        d = 评估(信号(low=None, high=None), 仅RR(1.5))
        self.assertTrue(d.passed)
        self.assertTrue(d.rr_skipped)

    def test_缺止损剔除(self):
        d = 评估(信号(stop=None), 仅RR(1.5))
        self.assertTrue(d.passed)
        self.assertTrue(d.rr_skipped)

    def test_缺止盈剔除(self):
        d = 评估(信号(target=None), 仅RR(1.5))
        self.assertTrue(d.passed)
        self.assertTrue(d.rr_skipped)

    def test_止损宽度恰20百分比进入RR(self):
        # mid=10, stop=8 → 宽度恰 20%，不剔除；target=11 → RR=0.5 < 1.5 → 拦截
        d = 评估(信号(stop=8.0, target=11.0), 仅RR(1.5))
        self.assertFalse(d.rr_skipped)
        self.assertFalse(d.passed)
        self.assertEqual(d.rule, "盈亏比")
        self.assertEqual(d.reward_risk, 0.5)

    def test_正常单不误拦(self):
        d = 评估(信号(stop=9.0, target=12.0), 仅RR(1.5))  # RR=2.0
        self.assertTrue(d.passed, d.reasons)
        self.assertFalse(d.rr_skipped)
        self.assertEqual(d.final_qty, 1000)


class D_P0与配置解析(unittest.TestCase):
    def test_兜底仅补缺(self):
        裸 = rc.PositionView("600760", 200, 48.97, None, "")
        stop, src = rc.fallback_stop(裸, rc.RiskConfig())
        self.assertEqual(stop, 46.52)
        有止损 = rc.PositionView("600760", 200, 48.97, 47.0, "signal")
        stop2, _ = rc.fallback_stop(有止损, rc.RiskConfig())
        self.assertEqual(stop2, 47.0)  # 不覆盖

    def test_组合配置解析(self):
        import json
        cfg = rc.RiskConfig.from_profile(json.loads(
            (Path(__file__).parent / "配置_RR10_全冷却5日.json").read_text(encoding="utf-8")))
        self.assertTrue(cfg.rr_filter_enabled)
        self.assertEqual(cfg.min_rr, 1.0)
        self.assertEqual(cfg.cooldown_scope, "all")
        self.assertEqual(cfg.cooldown_days, 5)
        self.assertFalse(cfg.fallback_stop_enabled)
        self.assertFalse(cfg.budget_enabled)
        self.assertFalse(cfg.min_amount_enabled)


class E_生产库真实坏价信号(unittest.TestCase):
    """只读抽查：真实坏价信号在 仅RR 配置下必须剔除告警且放行（不误拦）。"""

    @classmethod
    def setUpClass(cls):
        uri = (仓库根 / "data" / "stock_analysis.db").resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cls.信号165 = conn.execute(
                "SELECT id,stock_code,stop_loss,target_price,entry_low,entry_high "
                "FROM decision_signals WHERE id=165").fetchone()
            cls.坏价600519 = conn.execute(
                "SELECT id,stock_code,stop_loss,target_price,entry_low,entry_high "
                "FROM decision_signals WHERE stock_code LIKE '%600519%' "
                "AND stop_loss IS NOT NULL AND stop_loss < 100 LIMIT 5").fetchall()
        finally:
            conn.close()

    def test_600760信号165剔除且放行(self):
        s = self.信号165
        self.assertIsNotNone(s, "库中应有信号 #165")
        sv = rc.SignalView(s["id"], s["stock_code"], stop_loss=s["stop_loss"],
                           target_price=s["target_price"], entry_low=s["entry_low"],
                           entry_high=s["entry_high"])
        d = 评估(sv, 仅RR(1.5), price=48.97, qty=200)
        self.assertTrue(d.rr_skipped, f"应剔除 RR：{d}")
        self.assertTrue(d.passed, f"坏价剔除是告警不是拦截：{d.reasons}")

    def test_600519坏止损信号剔除且放行(self):
        if not self.坏价600519:
            self.skipTest("库中窗口内无 600519 坏止损信号样本（以合成样本用例为准）")
        for s in self.坏价600519:
            sv = rc.SignalView(s["id"], s["stock_code"], stop_loss=s["stop_loss"],
                               target_price=s["target_price"], entry_low=s["entry_low"],
                               entry_high=s["entry_high"])
            d = 评估(sv, 仅RR(1.5), price=float(s["entry_high"] or 1400), qty=100)
            self.assertTrue(d.rr_skipped, f"#{s['id']} 应剔除：{d}")
            self.assertTrue(d.passed, f"#{s['id']} 不应被拦：{d.reasons}")


if __name__ == "__main__":
    unittest.main(verbosity=2)