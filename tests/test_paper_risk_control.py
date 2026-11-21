# -*- coding: utf-8 -*-
"""纸面风控：坏目标 / 止盈空间两道闸门（默认关闭）。"""

from __future__ import annotations

import pytest

from src.services import paper_risk_control as rc
from src.services.paper_trading_service import resolve_policy


def _signal(*, target=None, stop=95.0, low=97.0, high=99.0) -> rc.SignalView:
    return rc.SignalView(
        signal_id=1,
        code="600000",
        stop_loss=stop,
        target_price=target,
        entry_low=low,
        entry_high=high,
    )


def _gate(signal: rc.SignalView, config: rc.RiskConfig, price: float = 100.0) -> rc.GateDecision:
    return rc.evaluate_entry(
        signal,
        price=price,
        intended_qty=100,
        trades=(),
        equity=100_000.0,
        cash=100_000.0,
        today="2026-11-18",
        trading_days=(),
        config=config,
    )


def test_empty_profile_keeps_bad_target_and_space_off():
    config = rc.RiskConfig.from_profile({"entry": {}, "exit": {}, "risk": {}})
    assert config.bad_target_enabled is False
    assert config.target_space_enabled is False
    assert _gate(_signal(target=None), config).passed is True


def test_bad_target_blocks_missing_and_inverted_targets():
    config = rc.RiskConfig(bad_target_enabled=True)

    missing = _gate(_signal(target=None), config)
    assert missing.passed is False
    assert missing.rule == "坏目标"

    inverted = _gate(_signal(target=99.5), config)
    assert inverted.passed is False
    assert inverted.rule == "坏目标"

    assert _gate(_signal(target=103.0), config).passed is True


def test_target_space_blocks_entries_too_close_to_target():
    config = rc.RiskConfig(target_space_enabled=True, min_target_space=0.015)

    too_close = _gate(_signal(target=101.0), config)
    assert too_close.passed is False
    assert too_close.rule == "止盈空间"
    assert "1.00%" in too_close.reasons[0]

    assert _gate(_signal(target=102.0), config).passed is True


def test_target_space_is_disabled_unless_configured():
    config = rc.RiskConfig()
    assert _gate(_signal(target=100.5), config).passed is True


def test_profile_keys_enable_gates_and_are_whitelisted():
    config = rc.RiskConfig.from_profile(
        {
            "entry": {"min_target_space": 0.02},
            "exit": {},
            "risk": {
                "stop_cooldown_scope": "all",
                "stop_cooldown_days": 3,
                "bad_target_enabled": True,
            },
        }
    )
    assert config.bad_target_enabled is True
    assert config.target_space_enabled is True
    assert config.min_target_space == pytest.approx(0.02)
    assert config.cooldown_scope == "all"
    assert config.cooldown_days == 3

    warnings: list[str] = []
    resolve_policy(
        {
            "entry": {"min_target_space": 0.02},
            "exit": {},
            "risk": {
                "stop_cooldown_scope": "all",
                "stop_cooldown_days": 3,
                "bad_target_enabled": True,
                "target_space_enabled": True,
            },
        },
        warnings=warnings,
    )
    assert not any("未启用" in message for message in warnings)
