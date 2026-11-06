import apiClient from './index';
import { toCamelCase } from './utils';
import type { PaperTradingRunRequest, PaperTradingRunResponse } from '../types/paperTrading';

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
};
