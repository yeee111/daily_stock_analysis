export type PaperTradingHorizon = 'intraday' | '1d' | '3d' | '5d' | '10d' | 'swing' | 'long';

export interface PaperTradingRunRequest {
  accountId?: number;
  accountName?: string;
  horizons?: PaperTradingHorizon[];
  minScore?: number;
  maxNames?: number;
  maxWeight?: number;
  signalDate?: string;
  dryRun?: boolean;
  force?: boolean;
  initialCash?: number;
}

export interface PaperTradingPlanItem {
  signalId?: number | null;
  stockCode: string;
  stockName?: string | null;
  action: string;
  score?: number | null;
  side: string;
  quantity: number;
  price?: number | null;
  referencePrice?: number | null;
  fee: number;
  tax: number;
  status: string;
  reason?: string | null;
}

export interface PaperTradingOrder {
  stockCode: string;
  side: string;
  quantity: number;
  price?: number | null;
  fee: number;
  tax: number;
  signalId?: number | null;
  note?: string | null;
  tradeUid?: string | null;
  tradeId?: number | null;
  status: string;
  reason?: string | null;
}

export interface PaperTradingAccountInfo {
  id?: number | null;
  name?: string | null;
  created: boolean;
  initialCash?: number | null;
}

export interface PaperTradingBatch {
  signalDates: string[];
  fillDate: string;
  referenceDate: string;
}

export interface PaperTradingPortfolioSummary {
  cash: number;
  marketValue: number;
  equity: number;
  perName: number;
  heldNames: number;
  cashAfterPlan: number;
}

export interface PaperTradingNoStopPosition {
  code: string;
  quantity: number;
  lastPrice?: number | null;
  signalId?: number | null;
  targetPrice?: number | null;
}

export interface PaperTradingStopDistance {
  code: string;
  lastPrice?: number | null;
  stopLoss: number;
  distancePct?: number | null;
}

export interface PaperTradingStopDistribution {
  within3Pct?: number;
  between35Pct?: number;
  between58Pct?: number;
  over8Pct?: number;
  unknownPrice?: number;
}

export interface PaperTradingPositionHealth {
  asOf: string;
  heldNames: number;
  coveredNames: number;
  noStopPositions: PaperTradingNoStopPosition[];
  stopDistances: PaperTradingStopDistance[];
  distanceDistribution: PaperTradingStopDistribution;
}

export interface PaperTradingRunResponse {
  status: string;
  dryRun: boolean;
  message: string;
  market: string;
  latestDataDate?: string | null;
  signalCounts: Record<string, number>;
  account?: PaperTradingAccountInfo | null;
  batch?: PaperTradingBatch | null;
  portfolio?: PaperTradingPortfolioSummary | null;
  items: PaperTradingPlanItem[];
  orders: PaperTradingOrder[];
  warnings: string[];
  corporateActions: string[];
  positionHealth?: PaperTradingPositionHealth | null;
}
