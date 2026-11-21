# -*- coding: utf-8 -*-
"""口径分离回归（2026-11-19）：交易胜率仅 long+completed；cash 三分类复用引擎 flat 中性带。

覆盖：cash 不进交易胜率、旧字段不变（追加不删旧）、三分类与 _classify_outcome
flat 分支阈值逐点一致（含 ±band 边界）、自定义中性带、缺失收益不计入、
平均模拟收益仅统计 long。逐行评判语义不在本测试范围（保持原样）。
"""

import unittest
from types import SimpleNamespace

from src.core.backtest_engine import BacktestEngine


def _row(*, position="long", outcome="win", ret=1.0, sim=1.0, status="completed", direction_correct=True):
    return SimpleNamespace(
        eval_status=status,
        position_recommendation=position,
        outcome=outcome,
        stock_return_pct=ret,
        simulated_return_pct=sim,
        direction_correct=direction_correct,
        hit_stop_loss=None,
        hit_take_profit=None,
        first_hit="neither",
        first_hit_trading_days=None,
        operation_advice="买入",
        eval_window_days=10,
        engine_version="v1",
    )


class TradeCashMetricsTestCase(unittest.TestCase):
    def test_cash_excluded_from_trade_win_rate(self):
        rows = [
            _row(position="long", outcome="win", ret=3.0, sim=2.0),
            _row(position="long", outcome="loss", ret=-3.0, sim=-1.0),
            _row(position="cash", outcome="win", ret=0.5, sim=0.0),      # 横盘正确
            _row(position="cash", outcome="loss", ret=5.0, sim=0.0),     # 错过上涨
        ]
        m = BacktestEngine.compute_trade_cash_metrics(rows, neutral_band_pct=2.0)
        self.assertEqual(m["trade_completed_count"], 2)
        self.assertEqual(m["trade_win_count"], 1)
        self.assertEqual(m["trade_win_rate_pct"], 50.0)
        self.assertEqual(m["trade_avg_simulated_return_pct"], 0.5)
        self.assertEqual(m["cash_flat_correct_count"], 1)
        self.assertEqual(m["cash_missed_up_count"], 1)
        self.assertEqual(m["cash_dodged_down_count"], 0)

    def test_old_summary_fields_unchanged(self):
        rows = [
            _row(position="long", outcome="win", ret=3.0, sim=2.0),
            _row(position="long", outcome="loss", ret=-3.0, sim=-1.0),
            _row(position="cash", outcome="win", ret=0.5, sim=0.0),
            _row(position="cash", outcome="loss", ret=5.0, sim=0.0),
        ]
        s = BacktestEngine.compute_summary(
            results=rows, scope="overall", code=None, eval_window_days=10, engine_version="v1",
            neutral_band_pct=2.0,
        )
        # 旧口径保持：win/loss 含 cash 方向评判，win_rate 仍按 win/(win+loss)
        self.assertEqual(s["completed_count"], 4)
        self.assertEqual(s["long_count"], 2)
        self.assertEqual(s["cash_count"], 2)
        self.assertEqual(s["win_count"], 2)
        self.assertEqual(s["loss_count"], 2)
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertEqual(s["direction_accuracy_pct"], 100.0)  # 四行 direction_correct 均 True（旧口径含 cash）
        self.assertEqual(s["avg_simulated_return_pct"], 0.25)  # (2-1+0+0)/4，旧口径含 cash
        # 新字段追加存在
        self.assertEqual(s["trade_win_rate_pct"], 50.0)
        self.assertEqual(s["trade_avg_simulated_return_pct"], 0.5)
        self.assertEqual(s["cash_missed_up_count"], 1)
        self.assertEqual(s["neutral_band_pct"], 2.0)

    def test_cash_classification_matches_engine_flat_threshold(self):
        band = 2.0
        for ret in (-5.0, -2.0001, -2.0, -1.0, 0.0, 1.0, 2.0, 2.0001, 5.0):
            outcome, _ = BacktestEngine._classify_outcome(
                stock_return_pct=ret, direction_expected="flat", neutral_band_pct=band,
            )
            m = BacktestEngine.compute_trade_cash_metrics(
                [_row(position="cash", outcome=outcome, ret=ret, sim=0.0)],
                neutral_band_pct=band,
            )
            if abs(ret) <= band:
                self.assertEqual(outcome, "win", f"ret={ret}")
                self.assertEqual(m["cash_flat_correct_count"], 1, f"ret={ret}")
                self.assertEqual(m["cash_missed_up_count"] + m["cash_dodged_down_count"], 0)
            elif ret > band:
                self.assertEqual(outcome, "loss", f"ret={ret}")
                self.assertEqual(m["cash_missed_up_count"], 1, f"ret={ret}")
            else:
                self.assertEqual(outcome, "loss", f"ret={ret}")
                self.assertEqual(m["cash_dodged_down_count"], 1, f"ret={ret}")

    def test_custom_neutral_band(self):
        m = BacktestEngine.compute_trade_cash_metrics(
            [_row(position="cash", outcome="win", ret=3.0, sim=0.0)], neutral_band_pct=5.0,
        )
        self.assertEqual(m["cash_flat_correct_count"], 1)
        self.assertEqual(m["cash_missed_up_count"], 0)
        self.assertEqual(m["neutral_band_pct"], 5.0)

    def test_missing_return_not_counted(self):
        m = BacktestEngine.compute_trade_cash_metrics(
            [_row(position="cash", outcome=None, ret=None, sim=None)], neutral_band_pct=2.0,
        )
        self.assertEqual(m["cash_missed_up_count"], 0)
        self.assertEqual(m["cash_dodged_down_count"], 0)
        self.assertEqual(m["cash_flat_correct_count"], 0)

    def test_insufficient_rows_excluded(self):
        m = BacktestEngine.compute_trade_cash_metrics(
            [
                _row(position="long", outcome="win", status="insufficient_data"),
                _row(position="cash", outcome="loss", ret=5.0, status="insufficient_data"),
            ],
            neutral_band_pct=2.0,
        )
        self.assertEqual(m["trade_completed_count"], 0)
        self.assertIsNone(m["trade_win_rate_pct"])
        self.assertEqual(m["cash_missed_up_count"], 0)

    def test_empty_rows(self):
        m = BacktestEngine.compute_trade_cash_metrics([], neutral_band_pct=2.0)
        self.assertEqual(m["trade_completed_count"], 0)
        self.assertIsNone(m["trade_win_rate_pct"])
        self.assertIsNone(m["trade_avg_simulated_return_pct"])


if __name__ == "__main__":
    unittest.main()