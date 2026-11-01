# 智能扫描 API

智能扫描把「批量分析」和「决策信号汇总」合成一次调用：提交一组股票代码，后台逐只跑完整 AI 分析流水线（不推送），结束后汇总本次扫描产生的决策信号，输出按动作与评分排序的买入排名。

当前为纯后端能力，不包含独立 Web 页面；页面接入需消费本文档的契约。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/smart-scan/scan` | 启动扫描，返回 `202` 与 `scan_id` |
| GET | `/api/v1/smart-scan/scan/{scan_id}` | 查询进度与最终结果 |
| GET | `/api/v1/smart-scan/history?limit=20` | 最近的扫描任务（`limit` 1–100，默认 20） |

### 启动请求

```json
{
  "stock_codes": ["600519", "000001", "300750"],
  "report_type": "detailed",
  "max_concurrent": 3
}
```

- `stock_codes`：1–50 个代码；空代码忽略，重复代码只分析一次。
- `report_type`：`simple` / `detailed` / `full` / `brief`，默认 `detailed`。
- `max_concurrent`：单次扫描内部的并发分析数，1–5，默认 3。

### 启动响应

```json
{
  "scan_id": "8f0c…",
  "trace_id": "8f0c…",
  "status": "pending",
  "message": "智能扫描已启动",
  "total_stocks": 3
}
```

`scan_id` 同时作为本次扫描的 `trace_id`，决策信号按该 trace 归属，不会混入其他分析。

## 结果结构

`GET /scan/{scan_id}` 在任务完成前返回任务状态；完成后额外返回 `aggregation`、`ranked_list` 和 `stock_results`。

### aggregation

| 字段 | 语义 |
| --- | --- |
| `total` | 输入条目数（含重复项、忽略空值） |
| `succeeded` / `failed` / `skipped` | 成功分析 / 分析失败 / 重复输入被跳过 |
| `action_breakdown` | 按最终动作计数，含未生成信号时回退的分析动作 |
| `score_distribution` | `strong_buy_gte80`、`buy_60_79`、`neutral_40_59`、`caution_20_39`、`avoid_lt20` |
| `avg_score` / `median_score` | 仅统计有信号的标的，无信号时为 `null` |

### ranked_list

每项包含 `rank`、`stock_code`、`stock_name`、`action`、`action_label`、`score`、`confidence`、`entry_low`、`entry_high`、`stop_loss`、`target_price`、`reason`、`risk_summary`、`catalyst_summary`、`signal_id`、`horizon`。

排序规则：

1. 动作优先级 `buy > add > watch > hold > reduce > sell > avoid > alert`；
2. 同动作内按 `score` 从高到低；
3. 无评分（信号缺失）排在所属动作之后。

一次分析可能产生多条信号（不同 horizon）。同一标的多条信号时只取最可执行的一条：先比动作优先级，再比评分。例如 `buy(60)` 优先于 `hold(95)`。

信号缺失（分析成功但未落库信号）时，动作回退到分析摘要中的动作，`reason` 固定为 `未生成决策信号`，不参与评分统计。

### stock_results

按输入顺序返回每个条目的结果：`success` / `failed` / `skipped`。重复出现的代码在第二次及之后标记为 `skipped`（`error=duplicate_input`），不会重复分析。

## 行为边界

- **失败隔离**：单只股票分析抛错或返回空结果只标记该标的 `failed`，其余标的继续；整体任务仍标记完成。
- **不推送**：扫描固定 `send_notification=false`，不触发通知渠道；需要推送请走常规分析或定时任务。
- **复用任务队列**：扫描作为通用后台任务运行，进度通过 `update_task_progress` 广播；任务被队列历史清理后查询返回 `404 smart_scan_not_found`。
- **信号翻页**：决策信号接口单页上限 100 条，扫描按 `total` 自动翻页，最多 20 页；超出上限时记录告警并停止翻页。
- **成本**：每只股票执行完整分析流水线（抓数据、检索、LLM）。50 只标的可能耗时较长且产生相应模型费用，建议先小批量验证。
- **不改选股 / 组合 / 通知配置**：扫描只产生分析报告与决策信号，不写入选股结果、不自动交易。

## 错误

| 状态 | 场景 |
| --- | --- |
| `422` | `stock_codes` 为空或超过 50、`max_concurrent` 越界、`report_type` 非法 |
| `404` | `scan_id` 不存在、已过期，或对应任务不是智能扫描 |

## 验证

```bash
python -m pytest tests/test_smart_scan_service.py tests/test_smart_scan_api.py -m "not network"
```

## 回滚

Revert 智能扫描模块（`api/v1/schemas/smart_scan.py`、`api/v1/endpoints/smart_scan.py`、`src/services/smart_scan_service.py`、router 注册与本文档）即可移除能力；无数据库迁移、无配置项新增。
