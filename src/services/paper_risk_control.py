# -*- coding: utf-8 -*-
"""纸面风控模块 —— 独立纯函数决策层（2026-11-16 策略优化，2026-11-17 按审查意见返工）

定位：输入 信号 + 持仓 + 成交流水 + 配置，输出 放行/拦截/兜底止损。
本模块不做任何 I/O：不连数据库、不读环境变量、不 import src/ 下任何代码，
可被干跑脚本、测试脚本或（经用户批准后）paper_trading_service.py 调用。

【B1 定案】默认全关：RiskConfig() 与 from_profile(空配置) 均为五项全关，
与生产策略档空配置兼容，保证默认零行为变化。任务书默认参数
（P0 5% / RR 1.5 / 预算 0.2×10 / 最小单 5000）由调用方显式传开关启用，
见 干跑_风控对比.py 的 任务书配置 与 接入说明.md 的示例配置。
灰度触发键白名单（仅新增风控键触发对应规则；既有 entry.max_names/max_weight
只是参数、不触发任何规则）：
    P0 兜底    ：exit.fallback_stop_loss_pct（别名 fallback_stop_pct）/ risk.fallback_stop_enabled
    RR 过滤    ：entry.min_rr（别名 min_reward_risk）/ risk.rr_filter_enabled
    出场冷却   ：risk.stop_cooldown_scope（必须显式给范围；risk.cooldown_enabled:false 强制关）
    现金预算   ：risk.cash_reserve_pct / risk.budget_enabled
    最小单金额 ：entry.min_order_amount / risk.min_amount_enabled
    坏目标     ：risk.bad_target_enabled（止盈缺失或 <= 拟成交价即拦）
    止盈空间   ：entry.min_target_space / risk.target_space_enabled
仅参数键（不触发启用）：entry.rr_max_stop_width、risk.stop_cooldown_days、
    entry.max_weight、entry.max_names。

七项风控（全部可配、全部可关）：
  P0 兜底止损   ：持仓无信号止损时，按 最新买入价×(1-兜底比例) 补一个止损价。
                  默认比例 5%。语义是"仅补缺"——现有 exit.stop_loss_pct 是覆盖
                  所有持仓（调研证实），本模块建议新增 exit.fallback_stop_loss_pct。
  P1 盈亏比过滤 ：RR = (止盈 - 入场中价) / (入场中价 - 止损)，入场中价 =
                  (entry_low + entry_high) / 2（裁决口径，不用参考日收盘）。
                  坏价样本剔除不进 RR：缺入场区间/缺止损/缺止盈/价格倒挂
                  （target=3.0/5.0 类坏目标）/止损宽度极端（600519 stop=5.0 类）
                  一律跳过该检查并告警，缺止损的持仓由 P0 兜底保护。
  P1 出场冷却   ：同标的出场后 N 个交易日内禁止回补，默认关闭。
                  范围 cooldown_scope: "off" / "stop_only"（仅止损出场）/ "all"。
                  拦截点在新信号买入候选循环（与 min_score 同层），不动 carried。
                  【S1】stop_only 依赖 exit_kind 准确：正式接入必须由引擎动作/
                  note 标记显式提供；价格贴合推断仅作干跑回退，跳空/滑点出场
                  （引擎按开盘成交）会被推断漏判，导致 stop_only 静默失效。
                  【S4】交易日历缺口 fail-open 但显式告警（warnings 参数 +
                  GateDecision.reasons / cooldown_unverified 标记）；调用方日历
                  窗口应 ≥ cooldown_days+1 个交易日且必须含今日。
  P2 现金预算   ：单笔预算 = min(max_weight×权益, 权益/目标持仓数)，
                  且保留 常备现金比例×权益 的现金（默认 5%，可配到 10%）。
                  预算只缩量、不放大：最终数量 = min(原策略拟买数量, 预算内数量)。
  P2 最小单金额 ：最终拟成交金额 < 5000 元跳过，消灭最低佣金侵蚀。
  P1.5 坏目标   ：信号缺止盈价，或止盈价 <= 拟成交价（价格倒挂）时拦截。
                  默认关闭。只信信号自身 target_price；策略档 exit.take_profit_pct
                  是持仓期覆盖价，不参与本检查。与 RR 的"坏价剔除"不同：这里
                  是拦截而不是跳过。
  P1.5 止盈空间 ：(止盈 - 拟成交价) / 拟成交价 < 最低空间（默认 1.5%）时拦截，
                  专治"开盘价已贴近止盈"的追高单；按实际成交价（含滑点）计算。

【S5 适用边界】clamp_buy_qty 的北交所分支按整百取整（上游 L327-328 是 ≥100
放行不取整），且对 intended_qty 做二次 clamp 会把科创板零股加仓归零——
现网 ALLOWED_BOARDS=main 无实际影响；放开 star/bse 板块前必须先修此差异，
"全关实例零行为变化"的承诺仅限主板口径（详见 接入说明.md）。

费用模型与 src/services/paper_trading_service.py L179-182 保持一致
（只读复制常量，保持本模块零依赖）：佣金万 2.5、最低 5 元；过户费万 0.1；
印花税万 5（仅卖出）。若上游费率调整，需同步这里（接入层建议加常量一致性断言）。

配置来源：策略档 paper_strategy_profiles.config_json 的 entry/exit/risk 三节，
键名映射见 RiskConfig.from_profile 的 docstring。现有策略档解析白名单
（_POLICY_ENTRY_KEYS 等，L493-495）之外的键会被静默忽略，正式接入需同步扩展
白名单，见 接入说明.md。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Mapping, Optional, Sequence, Tuple

# ----------------------------------------------------------------------
# 费用与整手规则（与 paper_trading_service.py 一致，只读复制）
# ----------------------------------------------------------------------
_COMMISSION_RATE = Decimal("0.00025")
_COMMISSION_MIN = Decimal("5.00")
_STAMP_RATE = Decimal("0.0005")
_TRANSFER_RATE = Decimal("0.00001")
_CENT = Decimal("0.01")


def to_cent(value: Any) -> Decimal:
    """金额四舍五入到分。"""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def compute_fees(side: str, gross: Any) -> Tuple[Decimal, Decimal]:
    """返回 (fee, tax)：fee=佣金+过户费，tax=卖出印花税。"""
    gross = to_cent(gross)
    commission = to_cent(gross * _COMMISSION_RATE)
    if commission < _COMMISSION_MIN:
        commission = _COMMISSION_MIN
    transfer = to_cent(gross * _TRANSFER_RATE)
    stamp = to_cent(gross * _STAMP_RATE) if side == "sell" else Decimal("0.00")
    return to_cent(commission + transfer), stamp


def board_of(code: str) -> str:
    """板块判定（与 paper_trading_service._board_of 的主板/创/科/北口径一致）。"""
    raw = str(code or "").strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")) and raw[2:].isdigit():
        raw = raw[2:]
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw.startswith("688"):
        return "star"
    if raw.startswith(("300", "301")):
        return "gem"
    if raw.startswith(("60", "00")):
        return "main"
    if raw.startswith(("4", "8")):
        return "bse"
    return "unknown"


def clamp_buy_qty(code: str, qty: int) -> int:
    """买入数量向下取整到合法整手；0 表示不可成交。

    【S5 边界】bse 按整百取整与上游（≥100 放行）不同；star 零股加仓会被归零。
    现网 ALLOWED_BOARDS=main 无影响，放开板块前须先对齐上游语义。
    """
    qty = int(qty)
    if board_of(code) == "star":
        return qty if qty >= 200 else 0
    return (qty // 100) * 100 if qty >= 100 else 0


# ----------------------------------------------------------------------
# 配置（B1：默认全关；仅新增风控键触发对应规则）
# ----------------------------------------------------------------------
COOLDOWN_SCOPES = ("off", "stop_only", "all")


@dataclass(frozen=True)
class RiskConfig:
    """七项风控配置。【B1 定案】开关默认全关（与生产空策略档兼容，零行为变化）；
    数值字段是"启用后的参数默认值"（任务书口径），本身不启用任何规则。"""

    # P0 兜底止损（仅补缺，不覆盖已有止损）
    fallback_stop_enabled: bool = False
    fallback_stop_loss_pct: float = 0.05
    # P1 盈亏比过滤（入场中价口径）
    rr_filter_enabled: bool = False
    min_rr: float = 1.5
    rr_max_stop_width: float = 0.20  # 止损宽度超过入场中价 20% 视为坏价；<=0 关闭该防护（仅参数，不触发启用）
    # P1 出场冷却（默认关闭；拦截点=新信号买入候选循环）
    cooldown_scope: str = "off"      # "off" / "stop_only" / "all"
    cooldown_days: int = 5           # 仅参数，不触发启用；范围必须显式配置
    # P2 现金预算
    budget_enabled: bool = False
    max_weight: float = 0.2          # 既有策略档键，仅参数，不触发启用
    target_names: int = 10           # 既有策略档键，仅参数，不触发启用
    cash_reserve_pct: float = 0.05   # 常备现金比例，任务书允许 5%~10%
    # P2 最小单金额
    min_amount_enabled: bool = False
    min_order_amount: float = 5000.0
    # P1.5 止盈计划（坏目标 / 最低止盈空间；按拟成交价计算）
    bad_target_enabled: bool = False    # 止盈缺失或 <= 拟成交价即拦截
    target_space_enabled: bool = False
    min_target_space: float = 0.015

    @classmethod
    def from_profile(cls, config: Optional[Mapping[str, Any]] = None) -> "RiskConfig":
        """从策略档 config_json（entry/exit/risk 三节）解析。

        【B1】空配置/None → 全关实例（零行为变化）。每项规则仅由其"触发键"
        显式启用（数值触发键须为有限正数；NaN/Infinity 视同未配置，S3 防御）：
          P0：exit.fallback_stop_loss_pct（别名 fallback_stop_pct）或 risk.fallback_stop_enabled
          RR：entry.min_rr（别名 min_reward_risk）或 risk.rr_filter_enabled
          冷却：risk.stop_cooldown_scope（off/stop_only/all；非法值回退 off；
                risk.cooldown_enabled:false 强制 off；仅配天数不启用）
          预算：risk.cash_reserve_pct 或 risk.budget_enabled
          最小金额：entry.min_order_amount 或 risk.min_amount_enabled
          坏目标：risk.bad_target_enabled
          止盈空间：entry.min_target_space 或 risk.target_space_enabled
        显式 *_enabled 布尔键优先于数值触发（true 可单独启用，false 一票否决）。
        仅参数键（不触发启用）：entry.rr_max_stop_width / risk.stop_cooldown_days /
        entry.max_weight / entry.max_names（后两者为既有语义键，绝不触发风控）。
        未知键忽略。正式接入需把这些新键加进策略档解析白名单，否则静默忽略。
        """
        cfg = config if isinstance(config, Mapping) else {}
        entry = cfg.get("entry") if isinstance(cfg.get("entry"), Mapping) else {}
        exit_ = cfg.get("exit") if isinstance(cfg.get("exit"), Mapping) else {}
        risk = cfg.get("risk") if isinstance(cfg.get("risk"), Mapping) else {}
        base = cls()

        def num(section: Mapping, key: str, alias: Optional[str] = None) -> Optional[float]:
            """取有限数值；缺失/非法/NaN/Infinity 一律返回 None（S3：视同未配置）。"""
            raw = section.get(key)
            if raw is None and alias:
                raw = section.get(alias)
            if raw is None:
                return None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        def flag(section: Mapping, key: str, default: bool) -> bool:
            value = section.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                return value.strip().lower() == "true"
            return default

        fallback_val = num(exit_, "fallback_stop_loss_pct", alias="fallback_stop_pct")
        min_rr_val = num(entry, "min_rr", alias="min_reward_risk")
        width_val = num(entry, "rr_max_stop_width")
        days_val = num(risk, "stop_cooldown_days")
        reserve_val = num(risk, "cash_reserve_pct")
        min_amt_val = num(entry, "min_order_amount")
        space_val = num(entry, "min_target_space")
        weight_val = num(entry, "max_weight")
        names_val = num(entry, "max_names")

        scope_raw = str(risk.get("stop_cooldown_scope") or "off").strip().lower()
        scope = scope_raw if scope_raw in COOLDOWN_SCOPES else "off"
        if not flag(risk, "cooldown_enabled", True):
            scope = "off"

        return cls(
            fallback_stop_enabled=flag(risk, "fallback_stop_enabled", fallback_val is not None and fallback_val > 0),
            fallback_stop_loss_pct=fallback_val if fallback_val is not None and fallback_val > 0 else base.fallback_stop_loss_pct,
            rr_filter_enabled=flag(risk, "rr_filter_enabled", min_rr_val is not None and min_rr_val > 0),
            min_rr=min_rr_val if min_rr_val is not None and min_rr_val > 0 else base.min_rr,
            rr_max_stop_width=width_val if width_val is not None else base.rr_max_stop_width,
            cooldown_scope=scope,
            cooldown_days=int(days_val) if days_val is not None and days_val > 0 else base.cooldown_days,
            budget_enabled=flag(risk, "budget_enabled", reserve_val is not None),
            max_weight=weight_val if weight_val is not None and weight_val > 0 else base.max_weight,
            target_names=max(1, int(names_val)) if names_val is not None and names_val > 0 else base.target_names,
            cash_reserve_pct=min(max(reserve_val, 0.0), 0.9) if reserve_val is not None else base.cash_reserve_pct,
            min_amount_enabled=flag(risk, "min_amount_enabled", min_amt_val is not None and min_amt_val > 0),
            min_order_amount=min_amt_val if min_amt_val is not None and min_amt_val > 0 else base.min_order_amount,
            bad_target_enabled=flag(risk, "bad_target_enabled", False),
            target_space_enabled=flag(risk, "target_space_enabled", space_val is not None and space_val > 0),
            min_target_space=space_val if space_val is not None and space_val > 0 else base.min_target_space,
        )


# ----------------------------------------------------------------------
# 输入视图（调用方负责从数据库/服务状态组装，本模块只读）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class SignalView:
    """一个待评估的开仓信号。RR 用入场中价；reference_close 仅留作展示/审计。"""

    signal_id: Optional[int]
    code: str
    name: str = ""
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    reference_close: Optional[float] = None
    is_carried: bool = False  # 仅展示用；裁决：拦截点在新信号循环，不动 carried 机制

    @property
    def entry_mid(self) -> Optional[float]:
        """入场中价 = (entry_low + entry_high) / 2；区间缺失/非法返回 None。"""
        lo, hi = self.entry_low, self.entry_high
        if lo is None or hi is None or lo <= 0 or hi <= 0 or hi < lo:
            return None
        return (lo + hi) / 2.0


@dataclass(frozen=True)
class PositionView:
    """一个当前持仓。stop_loss=None 且 stop_source='' 表示裸仓。"""

    code: str
    quantity: float
    last_buy_price: float
    stop_loss: Optional[float] = None
    stop_source: str = ""  # "signal" / "profile" / "fallback" / ""


@dataclass(frozen=True)
class TradeView:
    """一笔历史成交。exit_kind: 'stop_loss'/'take_profit'/None（卖出时有效）。

    【S1】exit_kind 应由调用方显式提供（引擎动作或 note 标记），
    用 resolve_exit_kind() 组装；价格推断仅限干跑回退。
    """

    code: str
    trade_date: str  # ISO 日期字符串
    side: str        # "buy" / "sell"
    quantity: float
    price: float
    exit_kind: Optional[str] = None


@dataclass(frozen=True)
class GateDecision:
    """开仓评估结果。passed=False 时 rule/reasons 给出拦截依据。"""

    passed: bool
    rule: str = ""                       # 首个命中的拦截规则
    reasons: Tuple[str, ...] = ()        # 全部命中说明（含坏价剔除/冷却日历告警）
    final_qty: int = 0                   # 放行时最终数量 = min(拟买, 预算内)；拦截时为 0
    budget_cap: Optional[float] = None   # 本单预算上限（元）
    reward_risk: Optional[float] = None  # 入场中价口径 RR（坏价/缺数据为 None）
    rr_skipped: bool = False             # RR 检查是否因坏价/缺数据被剔除
    cooldown_unverified: bool = False    # 【S4】冷却因日历缺口未能校验（fail-open 已告警）
    effective_stop: Optional[float] = None  # 参与 RR 计算的止损价（剔除时为 None）


# ----------------------------------------------------------------------
# P0：兜底止损（仅补缺）
# ----------------------------------------------------------------------
def fallback_stop(position: PositionView, config: RiskConfig) -> Tuple[Optional[float], str]:
    """返回 (有效止损价, 来源)。已有止损原样保留；裸仓补 买入价×(1-比例)。"""
    if position.stop_loss is not None and position.stop_loss > 0:
        return position.stop_loss, position.stop_source or "signal"
    if config.fallback_stop_enabled and position.last_buy_price > 0:
        stop = float(to_cent(Decimal(str(position.last_buy_price)) * (Decimal("1") - Decimal(str(config.fallback_stop_loss_pct)))))
        return stop, "fallback"
    return None, ""


def check_stop_trigger(
    position: PositionView,
    day_low: Optional[float],
    config: RiskConfig,
    *,
    day_open: Optional[float] = None,
) -> Optional[float]:
    """日内最低价触及有效止损时返回止损成交价，否则 None。

    【T1】传入 day_open 时按引擎跳空口径成交：开盘已低于止损 → 按开盘价成交
    （min(open, stop)，与上游 L2882 一致）；不传则退化为"止损价精确成交"的
    干跑简化假设（跳空日会高估成交价）。
    本函数只对"裸仓补兜底"的持仓有新增行为；已有信号止损的持仓由原引擎负责。
    """
    if day_low is None:
        return None
    stop, source = fallback_stop(position, config)
    if stop is None or source != "fallback":
        return None
    if float(day_low) > stop:
        return None
    if day_open is not None and float(day_open) > 0:
        return min(float(day_open), stop)
    return stop


# ----------------------------------------------------------------------
# P1：出场冷却（off / stop_only / all）
# ----------------------------------------------------------------------
def cooldown_remaining(
    code: str,
    today: str,
    trades: Sequence[TradeView],
    trading_days: Sequence[str],
    config: RiskConfig,
    *,
    warnings: Optional[List[str]] = None,
) -> int:
    """同标的最近一次（符合范围的）出场后，剩余冷却交易日数（0=不在冷却期）。

    范围：stop_only 只数止损出场；all 数任何卖出（含止盈/手动，exit_kind 未知也算）。
    口径：出场日 T 记 elapsed=0，返回 days-elapsed+1（**含今日**的剩余拦截天数，
    T+1..T+cooldown_days 拦截，出场当日回补也拦）。
    【S4】日历缺口 fail-open 但显式告警：日历为空/缺今日 → 整体未校验；
    出场日不在日历 → 该笔不计数。调用方日历窗口应 ≥ cooldown_days+1 个交易日
    且必须含今日。trading_days 为升序 ISO 日期字符串序列。
    """
    if config.cooldown_scope not in ("stop_only", "all") or config.cooldown_days <= 0:
        return 0

    def warn(message: str) -> None:
        if warnings is not None:
            warnings.append(message)

    day_index = {d: i for i, d in enumerate(trading_days)}
    if not day_index:
        warn("交易日历为空，冷却未校验（fail-open 放行）")
        return 0
    if today not in day_index:
        warn(f"今日 {today} 不在交易日历中，冷却未校验（fail-open 放行）")
        return 0
    last_exit_idx = None
    for trade in trades:
        if trade.code != code or trade.side != "sell":
            continue
        if config.cooldown_scope == "stop_only" and trade.exit_kind != "stop_loss":
            continue
        idx = day_index.get(trade.trade_date)
        if idx is None:
            warn(f"{code} 出场日 {trade.trade_date} 不在交易日历中，该笔未计入冷却（日历窗口应 ≥ cooldown_days+1 个交易日且含今日）")
            continue
        if idx <= day_index[today]:
            if last_exit_idx is None or idx > last_exit_idx:
                last_exit_idx = idx
    if last_exit_idx is None:
        return 0
    elapsed = day_index[today] - last_exit_idx
    if elapsed > config.cooldown_days:
        return 0
    return config.cooldown_days - elapsed + 1


def infer_exit_kind(sell_price: float, stop_loss: Optional[float], target_price: Optional[float], tol: float = 0.005) -> Optional[str]:
    """【仅限干跑回退】按成交价与止损/目标价的贴合度推断出场类型。

    局限（审查 S1）：跳空穿越止损/止盈时引擎按开盘价成交（上游 L2882/2884），
    滑点开启时所有出场价都偏离触发价——本推断会漏判为 None，使 stop_only
    冷却静默失效。正式接入禁止依赖本函数，必须用引擎动作/note 标记显式提供。
    """
    if stop_loss is not None and abs(sell_price - stop_loss) <= tol:
        return "stop_loss"
    if target_price is not None and abs(sell_price - target_price) <= tol:
        return "take_profit"
    return None


def resolve_exit_kind(
    *,
    explicit: Optional[str] = None,
    note: Optional[str] = None,
    sell_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    target_price: Optional[float] = None,
    allow_price_inference: bool = False,
) -> Optional[str]:
    """【S1】出场类型解析，优先级：显式动作 > note 标记 > 价格推断（仅干跑）。

    - explicit：引擎卖出动作（"stop_loss"/"take_profit"），正式接入唯一可信来源；
    - note：成交流水备注里的动作标记（如 "paper trading: signal #142 [stop_loss]"，
      或含"止损"/"止盈"字样），供 _order_note 追加标记后零 schema 改动使用；
    - 价格推断：仅 allow_price_inference=True（干跑回放）时启用，跳空/滑点会漏判。
    """
    if explicit in ("stop_loss", "take_profit"):
        return explicit
    text = str(note or "")
    if "[stop_loss]" in text or "止损" in text:
        return "stop_loss"
    if "[take_profit]" in text or "止盈" in text:
        return "take_profit"
    if allow_price_inference and sell_price is not None:
        return infer_exit_kind(sell_price, stop_loss, target_price)
    return None


# ----------------------------------------------------------------------
# 坏价检查：脏数据不进 RR 计算（裁决）
# ----------------------------------------------------------------------
def rr_price_quality(signal: SignalView, config: RiskConfig) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
    """返回 (入场中价, 止损, 止盈, 坏价说明)。说明为空串表示价格计划有效。

    剔除口径（调研《策略档能力与阈值依据》第二节）：
      - 缺入场区间（600760 #165 类）→ 无法锚定入场中价；
      - 缺止损（8 条历史样本）→ 走 P0 兜底，不进 RR；
      - 缺止盈 / 价格倒挂（target=3.0/5.0 坏目标、target<=中价、stop>=中价）；
      - 止损宽度 > rr_max_stop_width（600519 stop=5.0、宽度 99.6% 类极端值）。
    """
    mid = signal.entry_mid
    stop = signal.stop_loss if signal.stop_loss and signal.stop_loss > 0 else None
    target = signal.target_price if signal.target_price and signal.target_price > 0 else None
    if mid is None:
        return None, stop, target, "缺入场区间，无法计算入场中价"
    if stop is None:
        return mid, None, target, "无止损价（剔除 RR，持仓由 P0 兜底保护）"
    if target is None:
        return mid, stop, None, "无止盈价"
    if not (stop < mid < target):
        return mid, stop, target, f"价格倒挂/坏目标（止损 {stop}，中价 {mid:.3f}，止盈 {target}）"
    if config.rr_max_stop_width > 0 and (mid - stop) / mid > config.rr_max_stop_width:
        return mid, stop, target, f"止损宽度 {(mid - stop) / mid:.1%} 超过上限 {config.rr_max_stop_width:.0%}，疑似坏止损价"
    return mid, stop, target, ""


# ----------------------------------------------------------------------
# 开仓综合评估：冷却 → 盈亏比 → 预算/最小单金额
# ----------------------------------------------------------------------
def evaluate_entry(
    signal: SignalView,
    *,
    price: float,
    intended_qty: int,
    trades: Sequence[TradeView],
    equity: float,
    cash: float,
    today: str,
    trading_days: Sequence[str],
    config: RiskConfig,
) -> GateDecision:
    """评估一笔拟开仓（新信号买入候选循环内调用）。price=预估成交价。

    预算语义：只缩量不放大。final_qty = min(intended_qty, 预算内整手数量)。
    拦截优先级：出场冷却 > 坏目标 > 止盈空间 > 盈亏比 > 现金预算/最小单金额；
    全部命中项都记录。坏目标/止盈空间默认关闭，只看信号自身 target_price。
    坏价样本剔除 RR 检查（告警不拦截），缺止损的持仓由 P0 兜底。
    冷却日历缺口 fail-open 但写入 reasons 并置 cooldown_unverified=True（S4）。
    """
    reasons: List[str] = []
    blocked_rule = ""

    def block(rule: str, reason: str) -> None:
        nonlocal blocked_rule
        reasons.append(reason)
        if not blocked_rule:
            blocked_rule = rule

    # ---- P1 出场冷却（裁决：拦新信号，不动 carried；默认 off） ----
    cooldown_warnings: List[str] = []
    if config.cooldown_scope in ("stop_only", "all"):
        remaining = cooldown_remaining(signal.code, today, trades, trading_days, config, warnings=cooldown_warnings)
        if remaining > 0:
            范围 = "止损出场" if config.cooldown_scope == "stop_only" else "任何出场"
            block("出场冷却", f"{signal.code} {范围}后冷却期内（含今日剩 {remaining} 个交易日），拦截新信号回补")
    for message in cooldown_warnings:
        reasons.append(f"冷却告警：{message}")

    # ---- P1.5 止盈计划：坏目标 + 最低止盈空间（默认关闭，按拟成交价） ----
    target = signal.target_price if signal.target_price and signal.target_price > 0 else None
    if config.bad_target_enabled:
        if target is None:
            block("坏目标", f"{signal.code} 缺止盈价，无法评估止盈计划")
        elif target <= price:
            block("坏目标", f"{signal.code} 止盈 {target} ≤ 拟成交价 {price:.3f}（价格倒挂）")
    if config.target_space_enabled and target is not None and price > 0:
        space = (target - price) / price
        if space < config.min_target_space:
            block("止盈空间", f"{signal.code} 止盈空间 {space:.2%} < {config.min_target_space:.1%}"
                              f"（止盈 {target}，拟成交价 {price:.3f}）")

    # ---- P1 盈亏比过滤（裁决口径：入场中价） ----
    rr: Optional[float] = None
    rr_skipped = False
    effective_stop: Optional[float] = None
    if config.rr_filter_enabled:
        mid, stop, target, issue = rr_price_quality(signal, config)
        if issue:
            rr_skipped = True
            reasons.append(f"{signal.code} 坏价/缺数据剔除 RR 检查：{issue}")
        else:
            effective_stop = stop
            rr = round((target - mid) / (mid - stop), 3)
            if rr < config.min_rr:
                block("盈亏比", f"{signal.code} 盈亏比 {rr:.2f} < {config.min_rr}（入场中价 {mid:.3f}，止损 {stop}，止盈 {target}）")

    # ---- P2 现金预算 + 最小单金额 ----
    budget_cap: Optional[float] = None
    max_qty = clamp_buy_qty(signal.code, int(intended_qty))
    if config.budget_enabled and equity > 0:
        per_trade = min(config.max_weight * equity, equity / max(1, config.target_names))
        reserve = config.cash_reserve_pct * equity
        budget_cap = round(min(per_trade, max(0.0, cash - reserve)), 2)
        # 费用缓冲与 affordable_qty 的 0.995 口径一致
        affordable = int(budget_cap * 0.995 / price) if price > 0 else 0
        budget_qty = clamp_buy_qty(signal.code, affordable)
        if budget_qty <= 0:
            block("现金预算", f"{signal.code} 预算上限 {budget_cap:.2f} 元买不起一手（价格 {price}）")
        max_qty = min(max_qty, budget_qty)
    final_qty = max_qty
    gross = round(final_qty * price, 2)
    if final_qty <= 0 and not blocked_rule:
        block("现金预算", f"{signal.code} 可成交数量为 0")
    if config.min_amount_enabled and final_qty > 0 and gross < config.min_order_amount:
        block("最小单金额", f"{signal.code} 拟成交 {gross:.2f} 元 < {config.min_order_amount:.0f} 元（{final_qty} 股 × {price}）")

    passed = not blocked_rule
    return GateDecision(
        passed=passed,
        rule=blocked_rule,
        reasons=tuple(reasons),
        final_qty=final_qty if passed else 0,
        budget_cap=budget_cap,
        reward_risk=rr,
        rr_skipped=rr_skipped,
        cooldown_unverified=bool(cooldown_warnings),
        effective_stop=effective_stop,
    )