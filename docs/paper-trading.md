# 纸面交易（DSA 信号 → 组合账户）

纸面交易把 DSA 每天产出的 `DecisionSignal` 转成模拟成交，直接写进 DSA 现有的组合账户流水（PortfolioService），复用同一套持仓、现金、快照与风险计算。它不是新的回测引擎，也不改动信号生成和组合算法。

入口有三个：

| 入口 | 用途 |
| --- | --- |
| `/portfolio` 页面的「纸面交易」卡片 | 手动试跑或执行一次 |
| `POST /api/v1/paper-trading/run` | 脚本 / 外部调用 |
| `python scripts/paper_trading_run.py` | 命令行 / cron |

自动执行是可选钩子：`PAPER_TRADING_AUTO_RUN=true`（默认关闭）时，由两处触发——定时分析结束后（成功 / 跳过 / 失败 / 超时都会触发，每次运行只触发一次），以及独立定时 `PAPER_TRADING_SCHEDULE_TIMES`（默认 `18:05,19:00`）。两者共用同一套幂等层，重复触发不会重复成交。详见「自动触发」。

## 信号 → 动作

| 信号 action | 纸面动作 |
| --- | --- |
| `buy` / `add` | 建仓 / 加仓到等权目标金额 |
| `reduce` / `alert` | 卖出可卖数量的一半（向下取整到 100 股） |
| `sell` / `avoid` | 清仓（按参考日可卖数量） |
| `hold` / `watch` | 不动 |
| 其他未知动作 | 跳过并记录原因 |

同一代码同一批次有多条信号时，取 `created_at` 最新的一条。默认只认 `3d` / `5d` 周期的信号（`PAPER_TRADING_HORIZONS` 可改）。

仓位口径：

- 单票目标金额 = 参考日账户总权益 × `min(1 / max_names, max_weight)`
- 已持仓市值 ≥ 目标金额 × 0.8 视为已足额，不再加仓
- 持仓只数达到 `max_names` 时不再开新仓；买入候选按评分从高到低排序
- 评分低于 `min_score` 的买入信号跳过

## 时序：为什么按「成交日」分批

- 信号日 = 信号 `created_at` 所在自然日
- 成交日 = 信号日之后第一个 A 股交易日，按该日**开盘价**成交
- 参考日 = 成交日之前最后一个交易日；仓位、目标金额、参考价只用参考日及更早的数据

每次只处理「成交日 == A 股日线最新日期」的那一批。若只按最新信号跑，新信号会不断把未到期的旧批次顶掉，永远等不到行情。

行情没到成交日时**不下单**，接口返回 `status=data_not_ready`，CLI 退出码 2。早于最新数据日的漏跑批次会按成交日升序自动补跑（默认单次最多 5 个交易日），也可以用 `signal_date` 手动指定某一天。

## 交易规则

- 成交价：成交日开盘价；停牌（当日无日线）的标的直接跳过
- 可选成本模型：`PAPER_TRADING_SLIPPAGE_BPS`（单边基点）让买入上浮、卖出下调，并受涨跌停价约束；`PAPER_TRADING_MAX_ADV_PCT` 把单笔委托限制在该标的 `PAPER_TRADING_ADV_WINDOW_DAYS`（默认 20）日均量的指定百分比内，超出部分按整手裁剪，裁到不足一手则跳过。两者默认均为 `0`（关闭），现有账本口径不变
- T+1：卖出数量取自参考日持仓，成交日买入的股票当天不会被卖出
- 整手：主板 / 创业板按 100 股步长；科创板、北交所按 1 股步长、最低 200 / 100 股
- 涨跌停：开盘价触及涨停不买、触及跌停不卖；涨跌幅按主板 10%、创业板（300/301）与科创板（688）20%、北交所 30%
- 费用：佣金 0.025%（最低 5 元）+ 过户费 0.001%，卖出另收印花税 0.05%
- 现金不足时按可买数量缩量；仍不足一手则跳过
- 可交易板块：只有 `PAPER_TRADING_ALLOWED_BOARDS`（默认 `main,gem`）里的板块会开新仓。科创板（688）、北交所（4/8 开头）、港股（`HKxxxxx` / `xxxxx.HK`）的买入信号会在计划表里标「不在可交易板块，跳过」，不占用持仓名额、不消耗现金。卖出与止盈止损**不受限制**，已有持仓随时可以平掉，避免被锁在买不了的板块里。

## 止盈止损

建仓成交的 `note` 里记了开仓信号 id（`paper trading: signal #N`）。持仓后每次运行都会回读该信号的 `stop_loss` / `target_price`，作为常驻委托挂在持仓上。策略档配置了 `exit.stop_loss_pct` / `exit.take_profit_pct` 时，同一套规则改用「最近买入价 × (1 ± 比例)」的价位，覆盖信号价位：

- 当日最低价 ≤ 止损价 → 卖出全部持仓；成交价取 `min(开盘价, 止损价)`，跳空低开时按更差的开盘价成交
- 当日最高价 ≥ 目标价 → 卖出全部持仓；成交价取 `max(开盘价, 目标价)`，跳空高开时按更好的开盘价成交
- 两个价位同时触及（长下影 + 长上影）时止损优先，按保守口径处理
- 开盘一字跌停卖不出 → 继续持有，计划里标「触发止损但开盘跌停，卖不出」
- 当天已经因信号卖出（清仓/减半）的标的不会在同一开盘再被止盈止损或新买入信号重复开仓
- 止损单的 `trade_uid` 带 `-exit` 后缀（`paper-<成交日>-<代码>-sell-exit`），不会和同日信号卖单冲突

信号被重新评估后以最新一版为准；取不到信号或信号里的价位为空时该持仓不设止盈止损。常驻委托在**每个交易日**都参与计划，包括有新信号批次的日子（信号日）；只有当标的当天已被信号卖单清掉时才跳过。

### 时间止盈（持有到期）

策略档 `exit.max_holding_days`（正整数，默认不启用）给持仓加一道时间出口：从最近一次买入的次日开始数，**持有满 N 个交易日**后，在当天的开盘卖出全部持仓。

- 口径：买入日不计入天数；中间跨周末/节假日按交易日历只数交易日。例如周一买入、`max_holding_days=3`，则周四开盘卖出（周二/周三/周四为 3 个交易日）
- 成交顺序：**信号卖出 → 时间止盈 → 盘中止盈止损**。时间止盈按开盘价成交，因此先于当日的盘中触发；当天已被信号卖单清掉的标的不再重复卖
- 开盘跌停、停牌、流动性上限导致卖不出时保留持仓，计划里标明原因，下一个交易日继续判断（不会因为某一天卖不出就放弃）
- 卖出 `trade_uid` 带 `-time_exit` 后缀（`paper-<成交日>-<代码>-sell-time_exit`），与信号卖单、`-exit` 常驻委托互不冲突；成交 `note` 追加 `[time_exit]` 标记
- 交易日历不可用时不做时间止盈并写 warning（宁可不动，也不按错误天数强平）
- 计划审计 `max_holding_days` 记录本次生效值，便于事后核对

时间止盈解决的是「信号 horizon（如 3d）到期后仓位无限期扛单」：同一批信号在回放里，持有 3 日与持有 10 日的收益差远大于止盈位本身的差异。

## 持仓止损体检

每次运行（含干跑）结束都会附带一份 `position_health` 体检块，把「裸奔仓位」直接标出来：

- **无止损保护持仓**（`no_stop_positions`）：当前持仓里，最近一次建仓信号没有 `stop_loss` 的标的（只有目标价、信号被删、或手工录入无信号来源都算），附数量、现价、信号 id 与目标价；同时写一条 warning 提示
- **止损距离分布**（`stop_distances` / `distance_distribution`）：有止损的持仓按「止损价相对现价的百分比距离」列出，并按 |距离| 分桶：3% 以内 / 3–5% / 5–8% / 8% 以上 / 缺估值价
- 体检以最新数据日的持仓快照为准，只读不写；账户不存在时不附带该块；体检失败只记 warning，不影响本次运行
- `/portfolio` 纸面交易卡片在结果区展示体检块（红色列出无止损持仓 + 距离分布）

## 卖出复盘（历史平仓分析）

`/portfolio` 业绩看板下方的「卖出复盘」卡片（接口 `GET /api/v1/portfolio/sell-review`，支持 `account_id` / `as_of`）对每笔 FIFO 配对的平仓做退出归因，回答「为什么赚、为什么亏、卖得对不对」：

- **退出原因**：按成交 `trade_uid` 与信号回读判定——带 `-exit` 后缀的是止盈止损常驻委托，再按成交价与信号止损/目标价的关系细分（成交价不高于止损价记止损，其余记止盈）；不带后缀的按信号动作记信号清仓（sell/avoid）或信号减仓（reduce/alert）；无信号来源的成交记手动/其他
- **按原因统计**（`by_reason`）：每种退出原因的笔数、盈/亏、胜率、盈亏贡献、平均收益率与平均持有天数——看清利润来自哪类退出、亏损被哪类退出控制
- **建仓归因**：回读开仓信号的评分与理由（截断 160 字），在「最赚钱/最亏钱的几笔」块里展示，解释赚与亏的入场逻辑
- **卖后走势检验**（`post_sell_5d/10d/20d_pct`）：卖出后第 5/10/20 个交易日收盘相对卖价的涨跌幅，只读本地日线；结论（`exit_verdict`）取最长可用窗口——盈利笔分「落袋正确（卖后回落）/卖飞（卖后继续涨）」，亏损笔分「躲对下跌（卖后继续跌）/卖在低点（卖后反弹）」
- 只读、不触发网络请求；卖出后本地日线不足时对应窗口留空并提示，随数据积累逐步生效；已平仓不足 30 笔会标注结论仅供方向性参考；前复权日线在期间除权除息时可能与卖价口径有偏差

## 顺延重试

因开盘涨停（买入）、跌停或停牌（卖出）没成交的委托，可以在之后能成交的交易日补上：

- 只顺延 `PAPER_TRADING_RETRY_DAYS`（默认 3）个交易日以内的委托，超过上限放弃
- 中间每个交易日都必须「确实不可成交」（开盘涨跌停或停牌）才顺延；只要有一天其实能成交，就说明当天漏跑了，直接放弃并提示，不会用过期价格补单
- 顺延的委托按新成交日的前一交易日收盘重新计算参考价、仓位和数量，成交价仍用新成交日开盘价
- `trade_uid` 保留**原成交日**（`paper-<原成交日>-<代码>-<方向>`），同一天重复运行不会重复下单
- 顺延计入响应的 `signal_counts.carried`，`batch.signal_dates` 会显示原始信号日

## 补跑：漏掉的成交日

停机、分析失败或行情补给延迟都会留下「成交日已过、批次没跑」的窟窿。一次运行会先按成交日升序把这类批次补上：

- 只补 `成交日 < 最新数据日` 的批次，每个批次单独跑一遍完整流程（各自的开盘价、参考日、账户状态），账户状态按时间顺序演进
- 单次最多补 `PAPER_TRADING_CATCHUP_DAYS`（默认 5）个交易日；超出的部分下次运行继续
- 补跑结果汇总在响应的 `catchup` 字段（每日的 `fill_date` / `status` / 订单数），`signal_counts.catchup_days` 记录补了几天
- 已补过的批次靠 `trade_uid` 去重，重复运行返回 `duplicate_batch` 且不写入任何流水
- `dry_run=true` 时补跑也只是计划，不写数据库
- 补跑结束后，最新数据日那一批照常处理（同一次运行内完成）

## 自动触发

`PAPER_TRADING_AUTO_RUN=true` 时有两类触发，二者都走同一个幂等层：

| 触发 | 行为 |
| --- | --- |
| 定时分析结束 | 无论分析成功、跳过、失败、超时、worker 丢失还是 watchdog 异常，都会触发一次；分析因「另一次运行占用」而跳过、或本次运行被新的一代取代时不触发（下一次运行会触发） |
| 独立定时 | 按 `PAPER_TRADING_SCHEDULE_TIMES`（默认 `18:05,19:00`）执行，与分析的定时完全解耦：当天分析从没启动、被跳过或超时，纸面交易照常运行 |

每次运行都会记录触发来源（`analysis_hook:<outcome>` / `paper_trading_schedule` / `cli_hook:<outcome>`），便于在日志里确认是否重复触发。`PAPER_TRADING_AUTO_APPLY=false` 时自动触发只干跑。

## 分红送转

本地日线是前复权（qfq）：除权后历史价格会被整体缩放，如果不登记现金分红/送转，这笔收益会从浮动盈亏里静默消失。非干跑且 `PAPER_TRADING_CORPORATE_ACTIONS=true`（默认）时：

- 对当前持仓（有过买入成交）的每只代码拉取已实施的分配方案：东财 `stock_fhps_detail_em` → 新浪 `stock_history_dividend_detail` → 巨潮 `stock_dividend_cninfo` 逐级回退
- 只登记除权日落在「首次买入日 ~ 成交日」之间、且方案已实施的事件
- 现金分红按税前口径（每股派息），送转折算成持股倍数（10 送转 3 → 1.3），分别写入 `cash_dividend` / `split_adjustment`
- 按（代码、除权日、类型）去重，手动录入过的同一条不会再写
- 写入后组合缓存会从除权日起失效并重放，权益曲线、浮动盈亏与业绩看板随之包含这部分收益
- 本次新登记的条目在响应的 `corporate_actions` 里（人类可读，如 `600519 2026-06-26 每股派 28.0242 元`），`/portfolio` 卡片也会列出
- 拉取失败只记 warning，不影响下单

## 多账户与账户分支（Phase 1）

一个纸面账户就是一条独立账本；「分支」只描述它从哪个账户、哪一天分叉而来。Phase 1 的语义：

- 所有账户共享同一批决策信号：每次跑批只构建一次 `SignalBatch`（含补跑路径），账户数量不影响信号读取次数；
- 克隆账户在 fork 日与源账户逐字段一致，之后各账户独立发展、互不同步；
- 每个账户按自己的策略档（policy profile）执行，可以在同一批次上得出不同委托；
- 支持同时存在多个分支账户，跑批按 `account_id` 升序串行，SQLite 单写锁下不会互相踩写。

### 数据结构

| 表 | 作用 |
| --- | --- |
| `paper_strategy_profiles` | 命名策略档：`name` + `version` 唯一，`config_json` 固定三段 `entry` / `exit` / `risk` |
| `paper_account_branches` | 每个纸面账户一行：`parent_account_id`（主干为 NULL）、`fork_date`、`profile_id`、`label`、`auto_run` |
| `paper_account_runs` | 每次跑批每账户一行的审计：`batch_key`、`status`、策略档快照、`signal_ids`、`orders_json`、`error` |

三张表由服务启动时的 `Base.metadata.create_all` 自愈创建，不需要手写迁移；老库首次启动会为已有的纸面账户回填 branch 行与默认档，回填是幂等的，不会给主干账户写入非空策略。

### 克隆一个账户

```bash
# 默认 dry-run：先只读备份真实库到临时文件，在临时副本里建账户、复制、校验
python scripts/paper_trading_clone.py --name 纸面策略B --fork-date 2026-11-13

# 确认后真正写入；--profile-name 省略时用 default 档
python scripts/paper_trading_clone.py --source-account-id 2 --name 纸面策略B \
    --fork-date 2026-11-13 --profile-name branch-a --initial-cash 50000 --apply
```

- **dry-run 是默认值**：真实库以只读方式备份到临时文件，全部写入发生在该副本，输出里会打印源/克隆两边的快照比对；真实库一个字节都不写。
- `--apply` 才写真实库，写前打印待写内容；非交互终端下显式 `--apply` 视为确认。
- 复制的是**事件表**：`portfolio_trades`、`portfolio_cash_ledger`、`portfolio_corporate_actions` 中 `<= fork_date` 的行；`portfolio_positions`、`portfolio_position_lots`、`portfolio_daily_snapshots` 等派生表不复制，由回放重算。
- `fork_date` 会归一为「小于等于它的最后一个交易日」；交易日历不可用时沿用原值并在输出里说明。
- 拒绝的情况：源账户 `broker` 不是 `paper`、源账户没有任何事件、`fork_date` 早于首次活动、同名克隆已存在但 fork 日不同、名称已被无关账户占用、`fork_date` 晚于源账户最后成交日（除非 `--allow-future-fork`）。
- 幂等：同名且同 fork 日重复执行直接返回已有克隆，不新增任何行。
- 事务：建账户、复制三表、写 branch 行在同一个 `BEGIN IMMEDIATE` 事务内完成；省略 `--profile-id/--profile-name` 且库里还没有 `default` 档时，该档也在这个事务里创建（解析阶段只返回「待创建」，不会先写一行造成克隆失败后的孤立策略档）；行级自检（行数、逐行字段、金额精度）不通过则整体回滚，不留半个账户。
- 提交后校验：源账户与克隆账户各按 `as_of=fork_date` 跑一次 `get_portfolio_snapshot`，比对现金、权益、已实现盈亏、费用、税费（`round(,6)`）与持仓数量/成本（`round(,8)`）；不通过则补偿删除新账户的全部行并报错退出。
- `--initial-cash` 在 fork 之后的下一个交易日注入现金，不参与上述快照比对。
- 克隆不重算出场位（Phase 2）：fork 时已有的持仓沿用建仓信号里的绝对止损/目标价，或按所属账户策略档的 `exit.stop_loss_pct` / `exit.take_profit_pct` 在每次跑批时按该账户自己的最近买入价换算——两个账户因此可能得出不同价位，但「按 fork 时点分支成本把止盈止损位落库固定」属于后续阶段。

### 策略档

`config_json` 只有以下字段在当前阶段生效，缺省一律回退「环境变量 → 内置默认」：

| 段 | 字段 | 含义 |
| --- | --- | --- |
| `entry` | `horizons` | 信号周期，如 `"3d,5d"` |
| `entry` | `min_score` | 买入最低评分 |
| `entry` | `max_names` | 最多持有只数 |
| `entry` | `max_weight` | 单票最大权重 |
| `entry` | `initial_cash` | 自动建账户时的初始资金 |
| `risk` | `retry_days` / `catchup_days` | 未成交顺延天数 / 单次补跑上限 |
| `risk` | `slippage_bps` / `max_adv_pct` / `adv_window_days` | 滑点与流动性约束 |
| `exit` | `stop_loss_pct` / `take_profit_pct` | 按最近一次买入价换算的固定止损/止盈比例（`0 < x < 1`），覆盖信号自带的绝对价位；某一侧留空则回退信号值（移动止损、分批止盈、再入场冷却属于后续阶段） |
| `exit` | `max_holding_days` | 正整数，持有满 N 个交易日后在开盘卖出（时间止盈）；不配置则不做时间出口。语义见「时间止盈（持有到期）」 |

优先级为「显式调用参数 > 策略档 > 环境变量 > 内置默认」。未知字段忽略并给 warning；比例越界或非数字只告警并回退信号止损/止盈，不抛错；`config_json` 损坏时降级为空档，行为与单账户默认完全一致，不会让跑批崩溃。出场来源会写进计划审计（`exit_levels.<code>.source`，`profile`/`signal`）与卖出原因文案。

Phase 1 还没有策略档的 Web/API 写入口，用仓库层创建：

```python
from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import DatabaseManager

repo = PortfolioRepository(DatabaseManager.get_instance())
repo.create_profile(
    name="branch-a",
    config={
        "entry": {"horizons": "3d,5d", "min_score": 65, "max_names": 4, "max_weight": 0.25},
        "exit": {"stop_loss_pct": 0.08, "take_profit_pct": 0.20},
    },
)
```

### 自动跑批与开关

- `PAPER_TRADING_AUTO_RUN=true` 时，定时触发改为「构建一次信号批次 → 遍历 `auto_run=true` 的分支账户逐个成账」。
- 克隆出来的账户 `auto_run` 默认是 `false`：先观察，确认要参赛再把 `paper_account_branches.auto_run` 打开。
- `paper_account_branches.role` 只存 `live` / `control` 两个治理角色：主干与转正分支是 `live`，其余分支是 `control`；展示状态由 role + `auto_run` 派生 —— `live`、`control`（未参赛）、`shadow`（control 且 `auto_run=1`，正在影子运行）、`retired`（control 且 `auto_run=0`，已停跑对照）。老库启动时自动补 `role` 列并回填一次（主干 `live`、fork `control`），之后的重启不会覆盖转正结果。
- 主干账户没有 branch 行时，跑批回退到「按名称解析单个账户 + env/default 策略」，与改造前行为一致。
- 有 branch 行但全部 `auto_run=0` 时**不会**回退：返回 `multi_no_accounts`，一笔委托都不下，避免“暂停后又悄悄跑起来”。
- 回填 branch 行只在执行态（非 dry-run）发生；干跑只读不写，因此老库第一次干跑可能仍走上面那条主干回退路径，执行一次或手动回填后才进入多账户模式。
- 单账户异常只让该账户本次失败并记入 `paper_account_runs`，不阻断其它账户。账户解析或计划阶段失败时该账户不会写入任何成交；若进程在执行中途被杀，已提交的成交会保留，`trade_uid` 幂等保证重跑不会重复。

## API

`POST /api/v1/paper-trading/run`（需要管理员登录会话，`ADMIN_AUTH_ENABLED=true` 时）

```json
{
  "account_id": 2,
  "account_name": "纸面交易",
  "horizons": ["3d", "5d"],
  "min_score": 60,
  "max_names": 5,
  "max_weight": 0.2,
  "signal_date": "2026-10-31",
  "dry_run": true,
  "force": false,
  "initial_cash": 100000
}
```

除 `dry_run` 外均可省略。`dry_run=true`（默认）只返回计划，不写流水；`dry_run=false` 才成交。

`account_id` 与 `account_name` 都不传时，按名称 `纸面交易` 解析账户；不存在则在执行模式下自动创建，并以 `initial_cash`（默认 100000）记一笔「纸面交易初始资金」的现金流入。

多账户模式：把 `"all_accounts": true` 加进请求体即可。

```json
{
  "all_accounts": true,
  "dry_run": true
}
```

- 信号批次只构建一次，随后按 `account_id` 升序给每个 `auto_run=true` 的分支账户各成一次账；策略参数来自各账户自己的策略档。
- `all_accounts=true` 与单账户策略参数互斥（`account_id`、`account_name`、`horizons`、`min_score`、`max_names`、`max_weight`、`signal_date`、`initial_cash`），混用返回 `400 validation_error`，不会静默忽略。
- `force=true` 不是策略参数而是安全开关，可与 `all_accounts=true` 同用并对**每个账户**生效：只解除「同一成交日已有流水」的前置拒绝，`trade_uid` 幂等仍会跳过已成交委托。
- 响应保留原有字段，新增 `batch_key` 与 `accounts` 列表（每行含 `account_id`、`account_name`、`label`、`status`、`orders`、`profile_id/name/version`、`auto_run`、`warnings`）；计划态的 `items` / `orders` / `portfolio` / `account` 在多账户模式下为空，因为每个账户各有一份。`batch.signal_dates` 只列**本次到期桶**的信号创建日，未来成交日的信号和已过期补跑批次不在这里冒充本次窗口。
- 顶层 `status` 为 `multi_planned`（干跑）或 `multi_executed`（执行）。单个账户失败只体现为该行 `status=account_failed`，不影响其它账户。
- 干跑不写 `paper_account_runs`。执行态在写入任何成交**之前**先落一行 `status=running` 审计，成交结束再原地改成终态：进程若在成交提交与终态回填之间被杀，库里会留下 `running` 行作为「这次尝试已开始、成交可能已落库」的证据；执行阶段抛错时同一行改为 `account_failed` 并写入 `error`，不追加第二行。

### 运行审计

`GET /api/v1/paper-trading/runs?account_id=2&limit=20`（只读）返回该账户最近的执行审计：`batch_key`、`status`、`error`、实际计入的 `signal_ids`/`due_signal_ids`、`horizons`、出场位、生效的策略档快照与成交笔数，按写入顺序倒序。干跑不写审计，所以这里只会出现真正执行过的批次；执行中途被杀的尝试会留下 `status=running` 的行，据此可以判断成交是否可能已经落库，再用 `trade_uid` 幂等重跑收口。

### 跨账户对比与转正门槛

`GET /api/v1/paper-trading/branches/comparison?as_of=2026-11-14&lookback_days=180&cost_method=fifo`（只读）

- 每个分支账户的日度权益先按外部现金流水（入金/出金）还原为时间加权净值（TWR），再取「所有分支 fork_date 的最大值 → as_of」的共同交易日窗口，首日归一为 100，因此克隆账户次日注资不会被算成收益。
- 与 `role=live` 的参照账户同窗口对比：区间收益、最大回撤、超额、最近 20 日滚动胜率、跟踪偏离、连续榜首天数。
- 7 条转正门槛沿用影子框架默认阈值：可比 ≥ 20 日、成交 ≥ 8 笔、超额 ≥ 5%、回撤比 ≤ 1.2（live 回撤≈0 时改为分支回撤 ≤ 10%）、滚动胜率 ≥ 60%、连续榜首 ≥ 3 天；第 7 条「参数稳健性」在 DSA 分支上不适用（分支差异在出场纪律，没有可扰动的策略参数），返回 `status="na"` 且不参与 `passed` 判定。`score` 仍按影子框架权重计算，`na` 门槛按 0 分计，便于候选分支之间排序。
- `account_ids=2,3` 可只对比指定账户；live 参照仍会加载用于计算超额。找不到 live 时只返回各分支自身曲线，不算超额与门槛。
- 接口**只读**：不会为了补数据而写 `portfolio_daily_snapshots` 或审计行；净值来自已经存在的日度快照，因此依赖日常风控/纸面运行持续回填。若某账户没有历史快照，会在 `warnings` 中提示且该行 `series` 为空。

### 转正与降级

```bash
# 只读预演：打印目标分支的对比指标与 7 条门槛，不修改任何数据
python scripts/paper_branch_promote.py --account-id 3

# 真正写库：单事务完成 目标 -> live、其余 live -> control
python scripts/paper_branch_promote.py --account-id 3 --apply
```

- 转正只改治理角色，**不改 `auto_run`**：被降级的分支继续按原开关跑批，作为后续验证的对照组。
- 同一事务内完成降级、转正与自检（提交前必须恰好一个 `live`），任何异常整体回滚。
- 幂等：重复对同一分支 `--apply` 不会重复降级；对另一个分支再次 `--apply` 会把上一个 `live` 降为 `control`。
- 门槛未通过仍可执行（由操作者决策），脚本会先打印「未通过」提示；`auto_run=false` 的分支转正后不会自动参赛，脚本同样会提示。

### 响应状态

| status | 含义 |
| --- | --- |
| `planned` | 干跑完成，返回计划 |
| `executed` | 已写入成交 |
| `nothing_to_do` | 有批次但没有可执行委托 |
| `no_active_signals` | 没有活跃信号 |
| `no_due_batch` | 最新数据日没有到期批次（可能只有已过期的漏跑批次） |
| `data_not_ready` | 批次成交日的行情还没到（只处理未来批次，或指定批次尚未到期） |
| `duplicate_batch` | 该成交日已有流水，需 `force=true` 才继续 |
| `multi_planned` | 多账户干跑完成，逐账户结果在 `accounts` |
| `multi_executed` | 多账户执行完成，`accounts[].status` 给出每个账户的终态 |
| `multi_no_accounts` | 有分支记录但 `auto_run` 全为 0，本次不跑任何账户 |

### 幂等

每笔成交的 `trade_uid` 为 `paper-<成交日>-<代码>-<方向>`。重复执行同一批次不会重复写入；`force=true` 只是跳过「同日已有流水」的前置拦截，`trade_uid` 仍然兜底去重。

## 命令行

```bash
# 试跑（默认，不下单）
python scripts/paper_trading_run.py

# 真正写入
python scripts/paper_trading_run.py --apply

# 补跑某个信号日
python scripts/paper_trading_run.py --signal-date 2026-10-31 --apply

# 指定账户与参数
python scripts/paper_trading_run.py --account-id 2 --min-score 65 --max-names 5 --max-weight 0.2

# 原始 JSON
python scripts/paper_trading_run.py --json

# 多账户：每个 auto_run 分支账户按自己的策略档跑，信号只取一次
python scripts/paper_trading_run.py --all-accounts
python scripts/paper_trading_run.py --all-accounts --apply
```

`--all-accounts` 与单账户策略参数（`--account-id`、`--account-name`、`--horizons`、`--min-score`、`--max-names`、`--max-weight`、`--signal-date`、`--initial-cash`）互斥，混用直接报错退出；策略参数一律来自各账户的策略档。`--force` 不受此限，加了就对每个账户解除同日流水拒绝（`--all-accounts --apply --force`）。

退出码：`0` 成功或无操作，`2` 行情未到（`data_not_ready`），`3` 同日已有流水（`duplicate_batch`），`1` 有委托失败。

## Web 入口

在 `/portfolio` 页面选择账户（或保持「全部账户」）后：

- 「试跑（不下单）」只展示计划表、批次日期与计划后现金
- 「执行一次」先弹确认框，确认后写入模拟成交，并自动刷新账户、快照与流水

结果表会显示每只标的的方向、数量、参考价、状态（`planned` / `executed` / `skipped` / `rejected`）和跳过原因。

结果区还会展示「持仓止损体检」：红色列出无止损保护的持仓，并给出有止损持仓的距离分布（见上文）。

业绩看板下方另有「卖出复盘」卡片：按退出原因统计盈亏贡献、展示最赚/最亏几笔的建仓理由，并用卖后走势检验卖点（见上文）。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PAPER_TRADING_AUTO_RUN` | `false` | 自动运行总开关（分析结束 + 独立定时两类触发） |
| `PAPER_TRADING_AUTO_APPLY` | `true` | 自动运行是否真正成交；`false` 时为干跑 |
| `PAPER_TRADING_SCHEDULE_TIMES` | `18:05,19:00` | 独立于分析的定时触发时间，逗号分隔 |
| `PAPER_TRADING_CATCHUP_DAYS` | `5` | 单次最多补跑几个未执行的交易日（升序） |
| `PAPER_TRADING_ACCOUNT_NAME` | `纸面交易` | 默认纸面账户名 |
| `PAPER_TRADING_INITIAL_CASH` | `100000` | 自动创建账户时的初始资金 |
| `PAPER_TRADING_MIN_SCORE` | `60` | 买入最低评分 |
| `PAPER_TRADING_MAX_NAMES` | `5` | 最多持有只数 |
| `PAPER_TRADING_MAX_WEIGHT` | `0.2` | 单票最大权重 |
| `PAPER_TRADING_HORIZONS` | `3d,5d` | 参与交易的信号周期，逗号分隔 |
| `PAPER_TRADING_FETCH_MISSING_BARS` | `false` | 成交前是否先补拉缺失日线 |
| `PAPER_TRADING_RETRY_DAYS` | `3` | 未成交委托最多顺延几个交易日（`0` = 不顺延） |
| `PAPER_TRADING_CORPORATE_ACTIONS` | `true` | 非干跑时是否自动登记分红送转 |
| `PAPER_TRADING_ALLOWED_BOARDS` | `main,gem` | 允许开新仓的板块，逗号分隔：`main` / `gem` / `star` / `bse` / `hk`；卖出不受影响 |
| `PAPER_TRADING_SLIPPAGE_BPS` | `0` | 单边成交滑点（基点），买入上浮、卖出下调，受涨跌停价约束；`0` = 按开盘价成交 |
| `PAPER_TRADING_MAX_ADV_PCT` | `0` | 单笔委托不超过 N 日均量的百分比；`0` = 不限制，超出按整手裁剪 |
| `PAPER_TRADING_ADV_WINDOW_DAYS` | `20` | 计算平均成交量所用的交易日数 |

自动触发的两个接入点：运行时调度器（`src/services/runtime_scheduler.py`）与 `main.py --schedule`，两者都会注册分析结束回调与独立定时任务。手动 API / CLI 不受这些开关影响。

### 本机常驻配置

当前这台机器的常驻方式：

- `.env`：`SCHEDULE_ENABLED=true`、`SCHEDULE_RUN_IMMEDIATELY=false`、`PAPER_TRADING_AUTO_RUN=true`、`PAPER_TRADING_AUTO_APPLY=true`、`PAPER_TRADING_ALLOWED_BOARDS=main`（本机券商无科创板/创业板/北交所/港股权限，只做主板）
- 启动脚本：`启动DSA纸面交易.bat`（先检查 8000 端口，已在监听则直接退出）
- Windows 计划任务：`DSA纸面交易日更`，登录时启动上面的脚本

效果：工作日 18:00 定时分析 → 更新日线并生成信号 → 分析结束后自动执行纸面交易；18:05 / 19:00 还有独立定时兜底，分析没跑成也会补上。非交易日由 `TRADING_DAY_CHECK_ENABLED` 控制是否跳过分析。

## 排障

| 现象 | 处理 |
| --- | --- |
| `data_not_ready`，提示成交日行情没到 | 先跑一次分析更新 A 股日线，再重跑 |
| `no_due_batch`，提示还有交易日未补跑 | 正常现象：超出单次上限的部分下次运行继续；也可用 `signal_date=<指定日>` 手动补跑 |
| `duplicate_batch` | 该成交日已写过流水；确认无误时加 `--force` / `force=true` |
| 账户在「全部账户」视图创建不出 | 执行模式会用默认账户名自动创建，也可先手动建账户再传 `account_id` |
| 某标的被跳过 | 看计划表的说明列：停牌、涨跌停、一手不足、已足额、评分不足、行情缺失等 |
