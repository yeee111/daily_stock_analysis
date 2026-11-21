// 纸面分支对比（GET /api/v1/paper-trading/branches/comparison）响应类型。
// 与 api/v1/schemas/paper_trading.py 的 PaperBranchComparison* 一一对应（camelCase 化）。
// 只读接口：TWR 净值首日归一 100、共同分叉日窗口、7 条转正门槛（第 7 条 na）。

export interface BranchSeriesPoint {
  date: string;
  nav: number;
}

export interface BranchMetrics {
  days: number;
  returnPct: number | null;
  maxDrawdownPct: number | null;
  trades: number;
  excessPct: number | null;
  excess: number | null;
  rollingWin: number | null;
  trackingDev: number | null;
  topDays: number;
  referenceMaxDrawdownPct: number | null;
}

export interface BranchGateItem {
  name: string;
  value: number | null;
  threshold: string;
  ok: boolean | null;
  status: 'pass' | 'fail' | 'na';
  hint: string;
}

export interface BranchComparisonItem {
  accountId: number;
  accountName: string | null;
  label: string | null;
  role: string | null;
  state: string | null;
  parentAccountId: number | null;
  forkDate: string | null;
  profileId: number | null;
  profileName: string | null;
  profileVersion: number | null;
  autoRun: boolean | null;
  isReference: boolean;
  series: BranchSeriesPoint[];
  metrics: BranchMetrics;
  gates: BranchGateItem[];
  passed: boolean | null;
  score: number | null;
}

export interface BranchComparisonWindow {
  start: string | null;
  end: string | null;
  days: number;
  referenceAccountId: number | null;
}

export interface BranchComparisonResponse {
  asOf: string;
  costMethod: string;
  lookbackDays: number;
  thresholds: Record<string, number>;
  window: BranchComparisonWindow;
  branches: BranchComparisonItem[];
  warnings: string[];
}