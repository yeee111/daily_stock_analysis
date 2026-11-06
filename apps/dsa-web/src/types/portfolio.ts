import type { DecisionSignalItem } from './decisionSignals';

export type PortfolioCostMethod = 'fifo' | 'avg';
export type PortfolioSide = 'buy' | 'sell';
export type PortfolioCashDirection = 'in' | 'out';
export type PortfolioCorporateActionType = 'cash_dividend' | 'split_adjustment';

export interface PortfolioAccountItem {
  id: number;
  ownerId?: string | null;
  name: string;
  broker?: string | null;
  market: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  baseCurrency: string;
  isActive: boolean;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface PortfolioAccountListResponse {
  accounts: PortfolioAccountItem[];
}

export interface PortfolioAccountCreateRequest {
  name: string;
  broker?: string;
  market: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  baseCurrency: string;
  ownerId?: string;
}

export interface PortfolioPositionItem {
  symbol: string;
  market: string;
  currency: string;
  quantity: number;
  avgCost: number;
  totalCost: number;
  lastPrice: number;
  marketValueBase: number;
  unrealizedPnlBase: number;
  unrealizedPnlPct?: number | null;
  valuationCurrency: string;
  priceSource?: 'realtime_quote' | 'history_close' | 'missing' | string;
  priceProvider?: string | null;
  priceDate?: string | null;
  priceStale?: boolean;
  priceAvailable?: boolean;
  dataQuality?: 'ok' | 'partial' | string;
  limitations?: string[];
}

export interface PortfolioPositionAnalysisRequest {
  accountId?: number;
  analysisPhase?: 'auto' | 'premarket' | 'intraday' | 'postmarket';
  force?: boolean;
}

export interface PortfolioAccountSnapshot {
  accountId: number;
  accountName: string;
  ownerId?: string | null;
  broker?: string | null;
  market: string;
  baseCurrency: string;
  asOf: string;
  costMethod: PortfolioCostMethod;
  totalCash: number;
  totalMarketValue: number;
  /** Account market value converted into the parent snapshot currency. */
  totalMarketValueAggregate?: number | null;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  feeTotal: number;
  taxTotal: number;
  fxStale: boolean;
  dataQuality?: 'ok' | 'partial' | string;
  limitations?: string[];
  positions: PortfolioPositionItem[];
}

export interface PortfolioSnapshotResponse {
  asOf: string;
  costMethod: PortfolioCostMethod;
  currency: string;
  accountCount: number;
  totalCash: number;
  totalMarketValue: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  feeTotal: number;
  taxTotal: number;
  fxStale: boolean;
  dataQuality?: 'ok' | 'partial' | string;
  limitations?: string[];
  accounts: PortfolioAccountSnapshot[];
}

export interface PortfolioConcentrationItem {
  symbol: string;
  marketValueBase: number;
  weightPct: number;
  isAlert: boolean;
}

export interface PortfolioSectorConcentrationItem {
  sector: string;
  marketValueBase: number;
  weightPct: number;
  symbolCount: number;
  isAlert: boolean;
}

export interface PortfolioDrawdownBlock {
  seriesPoints: number;
  maxDrawdownPct: number;
  currentDrawdownPct: number;
  alert: boolean;
  fxStale: boolean;
}

export interface PortfolioStopLossItem {
  accountId: number;
  symbol: string;
  avgCost: number;
  lastPrice: number;
  lossPct: number;
  nearThresholdPct: number;
  isTriggered: boolean;
}

export interface PortfolioDecisionSignalRiskItem {
  accountId?: number | null;
  symbol: string;
  market: string;
  signal: Partial<DecisionSignalItem>;
}

export interface PortfolioDecisionSignalRiskBlock {
  available: boolean;
  total: number;
  actions: {
    sell?: number;
    reduce?: number;
    alert?: number;
    [key: string]: number | undefined;
  };
  items: PortfolioDecisionSignalRiskItem[];
}

export interface PortfolioRiskResponse {
  asOf: string;
  accountId?: number | null;
  costMethod: PortfolioCostMethod;
  currency: string;
  thresholds: Record<string, number>;
  concentration: {
    totalMarketValue: number;
    topWeightPct: number;
    alert: boolean;
    topPositions: PortfolioConcentrationItem[];
  };
  sectorConcentration: {
    totalMarketValue: number;
    topWeightPct: number;
    alert: boolean;
    topSectors: PortfolioSectorConcentrationItem[];
    coverage: Record<string, number>;
    errors: string[];
  };
  drawdown: PortfolioDrawdownBlock;
  stopLoss: {
    nearAlert: boolean;
    triggeredCount: number;
    nearCount: number;
    items: PortfolioStopLossItem[];
  };
  decisionSignalRisk?: PortfolioDecisionSignalRiskBlock;
}

export interface PortfolioPerformanceClosedTradeItem {
  stockCode: string;
  market: string;
  currency: string;
  openDate: string;
  closeDate: string;
  holdingDays: number;
  quantity: number;
  avgCost?: number | null;
  avgPrice?: number | null;
  costBasis: number;
  proceeds: number;
  pnl: number;
  returnPct?: number | null;
  fxStale: boolean;
}

export interface PortfolioPerformanceClosedTradeBlock {
  count: number;
  wins: number;
  losses: number;
  winRatePct?: number | null;
  avgReturnPct?: number | null;
  avgWinPct?: number | null;
  avgLossPct?: number | null;
  profitFactor?: number | null;
  realizedPnl: number;
  avgHoldingDays?: number | null;
  maxHoldingDays?: number | null;
  best?: PortfolioPerformanceClosedTradeItem | null;
  worst?: PortfolioPerformanceClosedTradeItem | null;
  items: PortfolioPerformanceClosedTradeItem[];
}

export interface PortfolioPerformancePositionItem {
  stockCode: string;
  market: string;
  currency: string;
  openDate: string;
  holdingDays: number;
  quantity: number;
  avgCost?: number | null;
  lastPrice?: number | null;
  costBasis: number;
  marketValue: number;
  unrealizedPnl: number;
  returnPct?: number | null;
  weightPct?: number | null;
  priceStale: boolean;
}

export interface PortfolioPerformancePositionBlock {
  count: number;
  unrealizedPnl: number;
  avgHoldingDays?: number | null;
  avgReturnPct?: number | null;
  items: PortfolioPerformancePositionItem[];
}

export interface PortfolioPerformanceEquityPoint {
  date: string;
  equity: number;
  cash: number;
  marketValue: number;
  drawdownPct: number;
  returnPct?: number | null;
  benchmarkReturnPct?: number | null;
}

export interface PortfolioPerformanceEquityCurve {
  points: PortfolioPerformanceEquityPoint[];
  maxDrawdownPct: number;
  currentDrawdownPct: number;
  fxStale: boolean;
}

export interface PortfolioPerformanceBenchmarkPoint {
  date: string;
  close: number;
  returnPct: number;
}

export interface PortfolioPerformanceBenchmark {
  available: boolean;
  symbol?: string | null;
  returnPct?: number | null;
  excessReturnPct?: number | null;
  series: PortfolioPerformanceBenchmarkPoint[];
  note?: string | null;
}

export interface PortfolioPerformanceSummary {
  netDeposits: number;
  totalEquity: number;
  totalCash: number;
  totalMarketValue: number;
  totalPnl: number;
  totalReturnPct?: number | null;
  realizedPnl: number;
  unrealizedPnl: number;
  feeTotal: number;
  taxTotal: number;
  investedRatioPct?: number | null;
  firstActivityDate?: string | null;
  daysActive?: number | null;
  maxDrawdownPct: number;
  currentDrawdownPct: number;
  positionCount: number;
  closedTradeCount: number;
  tradeCount: number;
  fxStale: boolean;
}

export interface PortfolioPerformanceResponse {
  asOf: string;
  accountId?: number | null;
  costMethod: PortfolioCostMethod;
  currency: string;
  summary: PortfolioPerformanceSummary;
  closedTrades: PortfolioPerformanceClosedTradeBlock;
  openPositions: PortfolioPerformancePositionBlock;
  equityCurve: PortfolioPerformanceEquityCurve;
  benchmark: PortfolioPerformanceBenchmark;
  notes: string[];
}

export interface PortfolioMarketDataPositionSync {
  asOf: string;
  synced: string[];
  failed: string[];
}

export interface PortfolioMarketDataIndexSync {
  symbol: string;
  synced: number;
  latestDate?: string | null;
  error?: string | null;
}

export interface PortfolioMarketDataRefreshResponse {
  asOf: string;
  positions: PortfolioMarketDataPositionSync;
  benchmark?: PortfolioMarketDataIndexSync | null;
}

export interface PortfolioTradeCreateRequest {
  accountId: number;
  symbol: string;
  tradeDate: string;
  side: PortfolioSide;
  quantity: number;
  price: number;
  fee?: number;
  tax?: number;
  market?: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  currency?: string;
  tradeUid?: string;
  note?: string;
}

export interface PortfolioCashLedgerCreateRequest {
  accountId: number;
  eventDate: string;
  direction: PortfolioCashDirection;
  amount: number;
  currency?: string;
  note?: string;
}

export interface PortfolioCorporateActionCreateRequest {
  accountId: number;
  symbol: string;
  effectiveDate: string;
  actionType: PortfolioCorporateActionType;
  market?: 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
  currency?: string;
  cashDividendPerShare?: number;
  splitRatio?: number;
  note?: string;
}

export interface PortfolioEventCreatedResponse {
  id: number;
}

export interface PortfolioDeleteResponse {
  deleted: number;
}

export interface PortfolioTradeListItem {
  id: number;
  accountId: number;
  tradeUid?: string | null;
  symbol: string;
  market: string;
  currency: string;
  tradeDate: string;
  side: PortfolioSide;
  quantity: number;
  price: number;
  fee: number;
  tax: number;
  note?: string | null;
  createdAt?: string | null;
}

export interface PortfolioTradeListResponse {
  items: PortfolioTradeListItem[];
  total: number;
  page: number;
  pageSize: number;
}

export interface PortfolioCashLedgerListItem {
  id: number;
  accountId: number;
  eventDate: string;
  direction: PortfolioCashDirection;
  amount: number;
  currency: string;
  note?: string | null;
  createdAt?: string | null;
}

export interface PortfolioCashLedgerListResponse {
  items: PortfolioCashLedgerListItem[];
  total: number;
  page: number;
  pageSize: number;
}

export interface PortfolioCorporateActionListItem {
  id: number;
  accountId: number;
  symbol: string;
  market: string;
  currency: string;
  effectiveDate: string;
  actionType: PortfolioCorporateActionType;
  cashDividendPerShare?: number | null;
  splitRatio?: number | null;
  note?: string | null;
  createdAt?: string | null;
}

export interface PortfolioCorporateActionListResponse {
  items: PortfolioCorporateActionListItem[];
  total: number;
  page: number;
  pageSize: number;
}

export interface PortfolioImportTradeItem {
  tradeDate: string;
  symbol: string;
  side: PortfolioSide;
  quantity: number;
  price: number;
  fee: number;
  tax: number;
  tradeUid?: string | null;
  dedupHash: string;
  currency?: string | null;
}

export interface PortfolioImportParseResponse {
  broker: string;
  recordCount: number;
  skippedCount: number;
  errorCount: number;
  records: PortfolioImportTradeItem[];
  errors: string[];
}

export interface PortfolioImportCommitResponse {
  accountId: number;
  recordCount: number;
  insertedCount: number;
  duplicateCount: number;
  failedCount: number;
  dryRun: boolean;
  errors: string[];
}

export interface PortfolioImportBrokerItem {
  broker: string;
  aliases: string[];
  displayName?: string;
}

export interface PortfolioImportBrokerListResponse {
  brokers: PortfolioImportBrokerItem[];
}

export interface PortfolioFxRefreshResponse {
  asOf: string;
  accountCount: number;
  refreshEnabled?: boolean;
  disabledReason?: string | null;
  pairCount: number;
  updatedCount: number;
  staleCount: number;
  errorCount: number;
}
