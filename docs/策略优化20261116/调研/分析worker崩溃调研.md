# DSA 晚间定时分析 worker_lost 根因调研

- 日期：2026-11-19
- 角色：股神开发团队 · 调研员（只读调研，未改任何共享代码，未执行 git 写操作）
- 仓库：`D:\qoder-D\qoder\daily_stock_analysis`
- 配套可复跑诊断：`诊断_worker退出挂起.py`（四部分，全程只读）、`诊断_WER事件.ps1`（只读事件日志查询）

## 0. 根因结论（一句话）

**晚间分析并没有崩溃：spawn 子进程完整跑完分析并 put 结果后，因遗留的非 daemon 线程被 Python 3.12 退出钩子无限 join 而退不出，watchdog 只等 2 秒（`join(2)`）即 `taskkill /T /F` 强杀并丢弃结果，误报为 `worker_lost`；"晚间无新信号/纸面断供"是 decision_signals 存 UTC 时间戳造成的误读，信号与纸面成交每天都在正常产生。**

两个任务前提被证据推翻：
1. "连续两天崩溃" → 实际自 11-09 起**每个交易日**都 worker_lost（共 8 次，见 §2），且每次分析都完整入库；
2. "decision_signals 停在 11-18 10:34 早间手动批，晚间无新信号" → 10:34 是 **UTC**，即本地 18:34 晚间批；不存在早间手动批（详见 §4）。

## 1. worker_lost 判定逻辑（问题 1）

代码：`src/services/runtime_scheduler.py`

- watchdog 在**父进程线程**中运行（L775-784），用 `multiprocessing.get_context("spawn")` 起子进程（L658-673），子进程 target 为 `_run_scheduled_analysis_process`（L90-115）：构造 `RuntimeSchedulerService` → `_run_analysis_locked` → `main.run_scheduled_analysis`，完成后 `result_queue.put({success/error/skipped})`。
- 结果收集循环（L677-687）：`queue.get(timeout=0.2)` 轮询；发现子进程死亡后把 deadline 缩到 +2s  drain 队列。
- **三条终局路径**：
  - 超时（L689-708）：默认 `DSA_RUNTIME_SCHEDULER_TIMEOUT_SECONDS`=2700s（45min，`.env` 未覆盖）。超时先记 `exceeded ... terminating worker` ERROR 日志再杀，outcome=`timeout`。**本次未命中**：18:00 起 32-34min 即结束，且日志无 exceeded 记录。
  - 路径 A（L710-720）：队列无结果且进程已死 → `_last_error="worker exited without a result (exit code X)"`，outcome=`worker_lost`。
  - **路径 B（L722-729，本次命中）**：`result` 已取到，但 `process.join(2)` 后仍存活 → `_terminate_analysis_process_tree`（Windows 走 `taskkill /PID /T /F`，L183-190）→ `_last_error="runtime scheduled analysis worker did not exit"`，outcome=`worker_lost`，**已到手的结果被直接丢弃**。
- 判别证据（只读 GET `/api/v1/system/scheduler/status`，服务自 11-09 重启后未再重启，状态即 11-18 晚现场）：
  `last_error="runtime scheduled analysis worker did not exit"`，`last_success_at=null`，`last_run_at=2026-11-18T18:00:00.232338`。
- **子进程 stdout/stderr 去向**：启动 bat（`启动DSA纸面交易.bat`）以 `python main.py --webui-only --port 8000 >> data\webui_startup.log 2>&1` 重定向；Windows spawn 子进程继承父进程标准句柄，故子进程 print/warnings/tqdm 也进 `data/webui_startup.log`（11-17/11-18 窗口可见 litellm "Provider List"、Tavily、pydantic warnings、akshare tqdm 等子进程输出）。`data/webui_startup.err.log`、`webui_stderr.log` 等是 11-10 之前旧启动方式的遗留 sink，与本窗口无关。
- **子进程没有文件日志**：`setup_logging` 只在 `main()` 内调用（main.py L1729 → L225）；spawn 子进程以 `__mp_main__` 重导入 main 模块但不执行 `main()`，`_run_analysis_locked`/`_reload_runtime_config` 也不初始化日志 → 子进程 logger 输出无 handler，WARNING 以上仅经 `logging.lastResort` 以无时间戳裸文本进 stderr。这解释了"debug 日志窗口内无分析进度"——**不是没跑，是子进程日志根本没接文件**。
- 误杀后果链：outcome=worker_lost ∈ `PAPER_TRADING_TRIGGER_OUTCOMES`（L52-59）→ 纸面 hook 仍触发；但 `_run_screening_signal_bridge` 仅在 SUCCESS 时运行（L637-638）→ **选股信号桥每晚被跳过**（真实功能损失）；`last_success_at` 永远 null → 看板/诊断长期误判"定时分析从未成功"。

## 2. 崩溃痕迹取证（问题 2）

- 全部 sinks（webui_startup.log / 旧 err、stderr、stdout / logs/stock_analysis*_20261117/18.log / restart_console.err.log）在 11-17、11-18 18:00-18:40 窗口**无任何 traceback**。子进程最后输出是正常业务日志（11-18 为 HK00700 数据源熔断降级），随后直接 worker_lost。
- **worker_lost 全量清单**（诊断脚本 Part D 实测）：11-09 18:36:41、11-10 18:32:45、11-11 18:32:02、11-12 18:34:09、11-13 18:35:00、11-16 18:33:58、11-17 18:32:15、11-18 18:34:14 —— 全部 18:3x，与"18:00 启动 + ~30min 分析时长 + join(2)"吻合。11-01~11-06 日志无 worker_lost。
- **时间耦合铁证**（只读 sqlite，analysis_history）：
  - 11-17：晚间入库 34 条，最后一条 HK00700 `18:32:11.409` → worker_lost `18:32:15`（≈join(2)+taskkill+日志）；
  - 11-18：晚间入库 34 条，最后一条 HK00700 `18:34:10.999` → worker_lost `18:34:14`。
  两晚分析都**完整跑完全部自选股+大盘复盘**，报告字段正常（sentiment_score 47-64、operation_advice 齐全、raw_result ~8KB）。
- **Windows 事件日志**（只读 Get-WinEvent，11-09~11-19）：Application Error / WER / Application Hang 在 **18:00-18:45 窗口 0 条**；全时段 **python 相关 WER 事件 0 条**；System 2004/2019/2020（资源耗尽）无事件。⇒ 排除原生崩溃（access violation / V8）、OOM、杀软强杀——与 watchdog `taskkill /F`（TerminateProcess，不留 traceback/WER）特征完全一致。
- 起点吻合：spawn watchdog 随 11-06 提交 `87ffb9c`（feat: add paper trading...）引入，`logs/webui_restart_20261109.log` 表明 11-09 重启部署 → **首个交易日起天天 worker_lost**。此前旧调度为进程内执行，无此问题。

## 3. 退出挂起机制（问题 3 核心 + 根因）

**挂起源（两处同构的"超时后不回收线程"设计）**：
1. `data_provider/efinance_fetcher.py` L193-213 `_ef_call_with_timeout`：efinance 内部 requests 无超时，用 `ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=30)` 包裹；超时后 `executor.shutdown(wait=False)`。注释自认："worker thread may continue running ... until the OS-level TCP timeout fires or the process exits"。eastmoney 大面积 RemoteDisconnected/黑洞期间（本窗口日志可见），超时路径必然留下挂死线程。
2. `src/agent/runner.py` L804 + finally（≈L887）：`pool.shutdown(wait=not timeout_triggered, cancel_futures=True)` —— 工具超时同样留线程（11-17 窗口 litellm 连续失败、Tavily 超额，agent 工具超时条件成立）。

**致命放大（Python 3.9+ 标准库行为，本机 3.12.10 实测）**：
- `concurrent/futures/thread.py`：executor 工作线程**非 daemon**，且 `threading._register_atexit(_python_exit)`；`_python_exit` 对所有注册线程做**无超时 `t.join()`**。
- 子进程业务完成 → `result_queue.put` → 解释器正常退出流程 → `_python_exit` join 挂死线程 → **永久阻塞**（Windows 已建立连接的 TCP 无数据可达小时级）。
- watchdog `join(2)` 必然超时 → taskkill → 路径 B worker_lost。

**隔离复现（诊断脚本 Part A，已实测通过）**：自建 spawn 子进程，仅挂一个 `Event.wait` 线程 + `shutdown(wait=False)` + put 结果：结果收到后 join(5) 仍存活（复现挂起）；无遗留线程对照组 0.12-0.23s 正常退出 exitcode=0。⇒ 机制与项目代码无关的部分成立，生产两处 fire-and-forget 线程池即触发器。

**spawn 重导入与环境继承（问题 3 其余项）**：
- 子进程重导入 main（`__mp_main__`，模块级 `setup_env()` 执行）+ litellm/akshare/pandas/httpx 等重模块；litellm 导入期同步 `httpx.get(model_prices_url, timeout=5)`（litellm/__init__.py L337-369，**非线程**，失败回退本地 JSON）→ 只增加导入耗时，不构成退出挂起。
- NO_PROXY：系统级含 `[::1]`（httpx InvalidURL 隐患），但启动 bat 显式 `set NO_PROXY=localhost,127.0.0.1` 覆盖；spawn 子进程继承父进程 `os.environ` → 继承的是修正值。`.env` 无 NO_PROXY/USE_PROXY/PROXY 项（实测 grep）；`setup_env`/Config 仅在存在 HTTP_PROXY 时向 NO_PROXY 合并国内域名（src/config.py L1545-1573），不会 reintroduce `[::1]`。且即便 InvalidURL 复现，也是可捕获异常 → `_run_analysis_locked` 兜底 put 结果 → outcome=failed 而非 worker_lost。**排除 NO_PROXY 为本次根因**。

## 4. "信号断供"澄清：UTC 时间戳误读（问题 4 关联）

- `decision_signals.created_at` 存 **UTC**；`analysis_history.created_at` 存**本地时间**。
- 铁证（诊断脚本 Part C 实测）：信号 `2026-11-18 10:34:11.003854 (HK00700)` 的 `source_report_id=534` 指向报告 `2026-11-18 18:34:10.999348` —— 差 8 小时零 4.5ms，正是"报告入库后立即提取信号"的时延。603893/601668 同样 4-7ms 耦合。
- 按日计数：11-16=30、11-17=32、11-18=31 条，UTC 10:05-10:34 = **本地 18:05-18:34 晚间批**。所谓"早间手动批"不存在；11-18 10:00-11:00 本地时段 analysis_history 为空（实测）。
- 附带误导：晚间信号 `trigger_source='cli'`（pipeline `query_source` 透传标签，具体赋值链未深查，标注为未知），进一步加深"手动批"误判。
- 结论：**信号供给从未中断**。纸面交易也每天成交（§5）。

## 5. 纸面交易批次竞态与幂等（问题 4 关联）

- 批次键：`_batch_key = f"{market}-{fill_date}-{sha1(sorted(signal_ids))[:12]}"`（paper_trading_service.py L824-833）。
- 11-18 实测时序：18:05 独立纸面批启动 → 18:12:34 `batch=cn-2026-11-18-10b92e7b4bcd` **executed（4+4+2+2 单）**；18:34:14 worker_lost → 18:42:31 分析后 hook 批 **同一批次键** → `duplicate_batch` 幂等拒绝。
- 解释（fill_date 语义由主会话核实）：fill=最新收盘日。18:05 批处理的是**上一交易日（11-17）信号**、fill=11-18 的 due 批；18:42 hook 重跑同一 due 批 → 键相同 → 拒绝。11-18 晚间新信号的 fill=11-19，属下一交易日 due 批——**T+1 设计使然，不是断供**。11-19 盘前干跑 due 批仍显示 11-18，因 11-19 尚未收盘、最新收盘日仍是 11-18。
- 残留观察项：18:05 独立批与分析（18:00-18:34）并行，若未来批次选择逻辑改为"当日最新信号"，会与未完成分析竞态；当前键含 signal_ids 摘要，天然幂等，风险低。

## 6. 对照实验与资源竞争（问题 4）

- **CLI 进程内对照实验：未执行**。主会话核实：手动 `main.py` 因 `SCHEDULE_ENABLED=true` 进入调度模式（data/手动补分析_20261119.log：`模式: 定时任务`、`启动时立即执行: False`、8000 端口被占 FastAPI 启动失败），未跑任何分析，已停止，0 新信号。该对照不成立，从证据链移除。
- 但对照已无必要：§2 的 DB 时序 + §3 的隔离复现独立证明"分析完成 + 退出挂起被误杀"，且 WER 0 事件排除崩溃类解释。
- 资源竞争/杀软：18:3x 主会话干跑/浏览器验证与团队活动**不构成杀进程路径**——taskkill 仅来自 watchdog 自身（代码内唯一 Windows 强杀点，L183-190）；无 OOM/资源耗尽事件；杀软拦静默安装的历史与运行中进程被杀无证据关联。排除。
- 附带风险提示（已发生，非假设）：手动 CLI 在调度模式下注册了**第二个调度器**（含 18:05 纸面独立批）。分析有 claim 去重（scheduled_analysis_claims），但纸面独立批无跨进程锁，若未停止会在 18:05 双跑。主会话已停止该进程，风险解除；建议后续手动补分析用一次性模式而非 --schedule。

## 7. 最小修复建议（ranked，只建议不改）

| # | 建议 | 改动点 | 影响面 | 风险 | 回滚 |
|---|------|--------|--------|------|------|
| 1 | **watchdog 先取结果再判退出**：L722 已拿到 result 时按 result.success/skipped 正常归类；join(2) 超时仅做进程树清理并记 WARNING（"worker slow exit, result preserved"），不再改判 worker_lost。可顺带 join(2)→join(10) | runtime_scheduler.py 单文件 ~10 行 | 仅调度终局归类；纸面 hook/选股桥立即恢复 SUCCESS 语义 | 低：清理逻辑不变，只是不丢结果 | revert 单文件 |
| 2 | **子进程日志接入**：`_run_scheduled_analysis_process` 开头调用 `setup_logging`（复用 config.log_dir），并把 exitcode/result 摘要写进 worker_lost 日志 | runtime_scheduler.py + 复用 logging_config | 子进程进度/异常从此可见，排障不再盲 | 低：多进程同文件 append 已有先例（父进程同写） | revert 单文件 |
| 3 | **消除遗留线程（治本）**：efinance helper 与 agent runner 超时路径改用 daemon 工作线程（标准库 ThreadPoolExecutor 不支持 daemon 参数，需换 `threading.Thread(daemon=True)` 单次封装或模块级自建 daemon 池），保持"调用方按时返回"语义不变 | efinance_fetcher.py L193-213、runner.py L804/finally | 数据源超时与 agent 工具超时行为不变；进程退出不再被 join 卡死 | 中：daemon 线程在退出时被硬终止，可能留未关闭连接（进程本就退出，可接受）；需回归测试超时路径 | 按文件 revert |
| 4 | **子进程退出兜底（备选，不与 3 叠加）**：put 结果后 `result_queue.close(); join_thread()` 确保 flush，再 `os._exit(0)` 跳过 atexit | runtime_scheduler.py L112-115 附近 | 绕过而非修复线程泄漏；跳过 atexit 可能丢未 flush 缓冲（分析已 commit，风险低） | 中低 | revert 单文件 |
| 5 | **时间戳口径统一（独立 issue）**：decision_signals.created_at 改本地或带时区 ISO；同步 `_parse_signal_day`、纸面 due 批判定、前端展示 | 信号服务+纸面+前端，需迁移评估 | 消除 UTC 误读（本次事故主因之一） | 高：涉及历史数据迁移与批次键语义，**不建议顺手改**，单独立项 | 需数据迁移回滚方案 |

推荐组合：**1+2 立即做**（恢复正确归类与观测，改动最小），**3 随后做**（治本），5 单独立项。1 生效后即使 3 未做，分析结果也不再被丢弃，选股桥恢复运行。

## 8. 事实/推断/未知分级

- **事实（实测）**：worker_lost 8 次清单；last_error 字符串；两晚 34 条报告入库与被杀时刻 2-4s 耦合；信号 UTC 4ms 耦合与按日计数；18:12 executed / 18:42 duplicate 同键；WER 窗口 0 事件；Part A 隔离复现；bat 重定向与 NO_PROXY 覆盖；.env 无代理/超时覆盖。
- **推断（机制成立、生产现场未直接观测）**：生产子进程退出挂起的具体线程来自 efinance/agent 超时池——依据代码机制+网络故障窗口+时序吻合+隔离复现，未在生产线程栈直接确认（只读约束不允许attach/复跑生产分析）。诊断脚本 Part A 可随时复核机制。
- **未知**：晚间信号 trigger_source='cli' 的赋值链；11-17/11-18 各自遗留线程的确切来源占比（efinance vs agent 工具）；11-09 之前旧调度是否也有退出慢问题（无 spawn 则无此路径）。
- **未验证项**：无（WER 查询已成功执行；CLI 对照实验标注未执行并移除）。

## 9. 证据索引

| 证据 | 位置 |
|------|------|
| worker_lost 两路径/超时/taskkill | src/services/runtime_scheduler.py L677-729、L183-190、L30、L52-59、L637-640 |
| 子进程入口与 put | 同上 L90-115；_run_analysis_locked L353-383 |
| 子进程无文件日志 | main.py L200-235、L1711/L1729（setup_logging 仅 main() 内） |
| 遗留线程两处 | data_provider/efinance_fetcher.py L193-213；src/agent/runner.py L804、finally `shutdown(wait=not timeout_triggered)` |
| atexit 无限 join | Python312/Lib/concurrent/futures/thread.py L23-37（_python_exit + _register_atexit） |
| litellm 导入期同步取价 | site-packages/litellm/__init__.py L337-369 |
| 启动重定向/NO_PROXY | 启动DSA纸面交易.bat；src/config.py L1545-1573 |
| 批次键 | src/services/paper_trading_service.py L824-833、L402-413 |
| 现场状态 | GET /api/v1/system/scheduler/status（last_error/last_success_at） |
| DB 时序 | data/stock_analysis.db（mode=ro）：analysis_history / decision_signals / source_report_id 耦合 |
| 日志窗口 | data/webui_startup.log L55240-55280（11-17）、L66560-66615（11-18）；logs/stock_analysis_debug_20261118.log L6947-6986 |
| WER | Get-WinEvent Application/System（11-09~11-19），诊断_WER事件.ps1 可复跑 |
| 部署起点 | git log：87ffb9c（11-06）引入 spawn watchdog；logs/webui_restart_20261109.log |

## 10. 复跑方式（PowerShell）

```powershell
& "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" "D:\qoder-D\qoder\daily_stock_analysis\docs\策略优化20261116\调研\诊断_worker退出挂起.py"
powershell -NoProfile -File "D:\qoder-D\qoder\daily_stock_analysis\docs\策略优化20261116\调研\诊断_WER事件.ps1" -Days 12
```

两脚本均只读：不写生产库、不触发分析、不导入项目模块（Part A 实验进程自建自清理）。