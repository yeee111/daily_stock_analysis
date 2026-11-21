import apiClient from './index';
import { toCamelCase } from './utils';
import type { PaperTradingRunRequest, PaperTradingRunResponse } from '../types/paperTrading';
import type { BranchComparisonResponse } from '../types/paperBranchCompare';

function toSnakeRunPayload(payload: PaperTradingRunRequest): Record<string, unknown> {
  return {
    account_id: payload.accountId,
    account_name: payload.accountName,
    horizons: payload.horizons,
    min_score: payload.minScore,
    max_names: payload.maxNames,
    max_weight: payload.maxWeight,
    signal_date: payload.signalDate,
    dry_run: payload.dryRun,
    force: payload.force,
    initial_cash: payload.initialCash,
  };
}

export const paperTradingApi = {
  async run(payload: PaperTradingRunRequest): Promise<PaperTradingRunResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/paper-trading/run',
      toSnakeRunPayload(payload),
    );
    return toCamelCase<PaperTradingRunResponse>(response.data);
  },

  // 只读：跨账户分支净值与转正门槛对比（不写任何快照/审计）
  async comparison(params?: {
    asOf?: string;
    lookbackDays?: number;
    accountIds?: string;
  }): Promise<BranchComparisonResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/paper-trading/branches/comparison',
      {
        params: {
          as_of: params?.asOf,
          lookback_days: params?.lookbackDays,
          account_ids: params?.accountIds,
        },
      },
    );
    return toCamelCase<BranchComparisonResponse>(response.data);
  },
};
