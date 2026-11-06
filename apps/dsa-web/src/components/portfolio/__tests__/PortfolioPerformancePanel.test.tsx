import type React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { PortfolioPerformancePanel } from '../PortfolioPerformancePanel';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';

const { getPerformance, refreshMarketData } = vi.hoisted(() => ({
  getPerformance: vi.fn(),
  refreshMarketData: vi.fn(),
}));

vi.mock('../../../api/portfolio', () => ({
  portfolioApi: { getPerformance, refreshMarketData },
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

function makeReport(overrides: Record<string, unknown> = {}) {
  return {
    asOf: '2026-11-03',
    accountId: 42,
    costMethod: 'fifo',
    currency: 'CNY',
    summary: {
      netDeposits: 100000,
      totalEquity: 102220.01,
      totalCash: 823.01,
      totalMarketValue: 101397,
      totalPnl: 2220.01,
      totalReturnPct: 2.22,
      realizedPnl: 300,
      unrealizedPnl: 1920.01,
      feeTotal: 35.99,
      taxTotal: 0,
      investedRatioPct: 99.19,
      firstActivityDate: '2026-10-30',
      daysActive: 5,
      maxDrawdownPct: 1.25,
      currentDrawdownPct: 0.2,
      positionCount: 1,
      closedTradeCount: 1,
      tradeCount: 2,
      fxStale: false,
    },
    closedTrades: {
      count: 1,
      wins: 1,
      losses: 0,
      winRatePct: 100,
      avgReturnPct: 3,
      avgWinPct: 3,
      avgLossPct: null,
      profitFactor: null,
      realizedPnl: 300,
      avgHoldingDays: 2,
      maxHoldingDays: 2,
      best: null,
      worst: null,
      items: [
        {
          stockCode: '600519',
          market: 'cn',
          currency: 'CNY',
          openDate: '2026-10-30',
          closeDate: '2026-11-01',
          holdingDays: 2,
          quantity: 100,
          avgCost: 100,
          avgPrice: 103,
          costBasis: 10000,
          proceeds: 10300,
          pnl: 300,
          returnPct: 3,
          fxStale: false,
        },
      ],
    },
    openPositions: {
      count: 1,
      unrealizedPnl: 1920.01,
      avgHoldingDays: 1,
      avgReturnPct: 1.92,
      items: [
        {
          stockCode: '601208',
          market: 'cn',
          currency: 'CNY',
          openDate: '2026-11-02',
          holdingDays: 1,
          quantity: 5900,
          avgCost: 17.0,
          lastPrice: 17.33,
          costBasis: 100300,
          marketValue: 102225,
          unrealizedPnl: 1920.01,
          returnPct: 1.92,
          weightPct: 99.5,
          priceStale: false,
        },
      ],
    },
    equityCurve: {
      points: [
        { date: '2026-11-02', equity: 100167.14, cash: 14029.14, marketValue: 86138, drawdownPct: 0, returnPct: 0.17, benchmarkReturnPct: null },
        { date: '2026-11-03', equity: 102220.01, cash: 823.01, marketValue: 101397, drawdownPct: 0, returnPct: 2.22, benchmarkReturnPct: null },
      ],
      maxDrawdownPct: 1.25,
      currentDrawdownPct: 0.2,
      fxStale: false,
    },
    benchmark: {
      available: false,
      symbol: '000300',
      returnPct: null,
      excessReturnPct: null,
      series: [],
      note: '本地无 000300 的指数日线，基准对比暂缺',
    },
    notes: [
      '本地无 000300 的指数日线，基准对比暂缺',
      '成交按 T+1 开盘价撮合，未模拟排队/滑点，实盘结果会偏保守',
    ],
    ...overrides,
  };
}

function renderPanel() {
  return render(
    <UiLanguageProvider>
      <PortfolioPerformancePanel accountId={42} costMethod="fifo" includeRealtime={false} />
    </UiLanguageProvider>,
  );
}

describe('PortfolioPerformancePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
    getPerformance.mockResolvedValue(makeReport());
    refreshMarketData.mockResolvedValue({
      asOf: '2026-11-04',
      positions: { asOf: '2026-11-04', synced: ['601208', '600519'], failed: [] },
      benchmark: { symbol: '000300', synced: 250, latestDate: '2026-11-04', error: null },
    });
  });

  it('renders headline metrics from the report', async () => {
    renderPanel();

    await waitFor(() => expect(screen.getByText('+2.22%')).toBeTruthy());
    expect(screen.getByText('CNY 102,220.01')).toBeTruthy();
    expect(screen.getByText('100.00%')).toBeTruthy();
    expect(screen.getByText('1.25%')).toBeTruthy();
    expect(getPerformance).toHaveBeenCalledWith({
      accountId: 42,
      asOf: undefined,
      costMethod: 'fifo',
      includeRealtime: false,
    });
  });

  it('renders open position and closed trade rows', async () => {
    renderPanel();

    await waitFor(() => expect(screen.getByText('601208')).toBeTruthy());
    expect(screen.getByText('600519')).toBeTruthy();
    expect(screen.getByText('2026-11-02')).toBeTruthy();
    expect(screen.getByText('2026-10-30')).toBeTruthy();
  });

  it('surfaces the benchmark note and methodology notes', async () => {
    renderPanel();

    await waitFor(() => expect(screen.getByText('本地无 000300 的指数日线，基准对比暂缺')).toBeTruthy());
    expect(screen.getByText('成交按 T+1 开盘价撮合，未模拟排队/滑点，实盘结果会偏保守')).toBeTruthy();
  });

  it('reloads when the refresh button is clicked', async () => {
    renderPanel();

    await waitFor(() => expect(getPerformance).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(getPerformance).toHaveBeenCalledTimes(2));
  });

  it('syncs market data, switches to live prices and reloads on 刷新行情', async () => {
    renderPanel();

    await waitFor(() => expect(getPerformance).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '刷新行情' }));

    await waitFor(() => expect(refreshMarketData).toHaveBeenCalledWith({ accountId: 42, asOf: undefined }));
    await waitFor(() => expect(getPerformance).toHaveBeenCalledTimes(2));
    expect(getPerformance).toHaveBeenLastCalledWith({
      accountId: 42,
      asOf: undefined,
      costMethod: 'fifo',
      includeRealtime: true,
    });
    expect(
      screen.getByText('持仓已按实时价估值 · 已补齐 2 只持仓日线 · 基准 000300 新增 250 根日线'),
    ).toBeTruthy();
  });

  it('reports the benchmark as current when the sync adds no bars', async () => {
    refreshMarketData.mockResolvedValue({
      asOf: '2026-11-04',
      positions: { asOf: '2026-11-04', synced: [], failed: [] },
      benchmark: { symbol: '000300', synced: 0, latestDate: '2026-11-04', error: null },
    });
    renderPanel();

    await waitFor(() => expect(getPerformance).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '刷新行情' }));

    expect(
      await screen.findByText('持仓已按实时价估值 · 基准 000300 日线已是最新'),
    ).toBeTruthy();
  });

  it('shows an alert when the request fails', async () => {
    getPerformance.mockRejectedValue(new Error('boom'));
    renderPanel();

    await waitFor(() => expect(screen.getByText('业绩数据加载失败')).toBeTruthy());
    expect(screen.getByText('boom')).toBeTruthy();
  });
});
