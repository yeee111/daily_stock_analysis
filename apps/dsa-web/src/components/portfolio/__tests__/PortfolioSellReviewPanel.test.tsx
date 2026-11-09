import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { PortfolioSellReviewPanel } from '../PortfolioSellReviewPanel';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';

const { getSellReview } = vi.hoisted(() => ({
  getSellReview: vi.fn(),
}));

vi.mock('../../../api/portfolio', () => ({
  portfolioApi: { getSellReview },
}));

function makeItem(overrides: Record<string, unknown> = {}) {
  return {
    stockCode: '600887',
    stockName: '伊利股份',
    market: 'cn',
    openDate: '2026-11-02',
    closeDate: '2026-11-06',
    holdingDays: 4,
    quantity: 600,
    avgCost: 30.1,
    avgPrice: 31.5,
    pnl: 790.18,
    returnPct: 4.37,
    exitReason: 'take_profit',
    exitReasonLabel: '止盈触发',
    entrySignalId: 20,
    entryScore: 68,
    entryReason: '业绩超预期叠加资金流入',
    stopLoss: 27.8,
    targetPrice: 31.5,
    postSellSessions: 6,
    postSell5dPct: -2.5,
    postSell10dPct: null,
    postSell20dPct: null,
    exitVerdict: 'locked_gain',
    exitVerdictLabel: '落袋正确（卖后回落）',
    verdictHorizon: 5,
    ...overrides,
  };
}

function makeReview(overrides: Record<string, unknown> = {}) {
  const winner = makeItem();
  const loser = makeItem({
    stockCode: '600028',
    stockName: '中国石化',
    closeDate: '2026-11-05',
    holdingDays: 1,
    pnl: -39.3,
    returnPct: -6.71,
    exitReason: 'stop_loss',
    exitReasonLabel: '止损触发',
    entryReason: '低估值防御',
    postSell5dPct: -1.2,
    exitVerdict: 'avoided_loss',
    exitVerdictLabel: '躲对下跌（卖后继续跌）',
  });
  return {
    asOf: '2026-11-09',
    accountId: 42,
    summary: {
      count: 2,
      wins: 1,
      losses: 1,
      winRatePct: 50,
      realizedPnl: 750.88,
      avgReturnPct: -1.17,
      avgHoldingDays: 2.5,
    },
    byReason: [
      { reason: 'take_profit', label: '止盈触发', count: 1, wins: 1, losses: 0, winRatePct: 100, totalPnl: 790.18, avgReturnPct: 4.37, avgHoldingDays: 4 },
      { reason: 'stop_loss', label: '止损触发', count: 1, wins: 0, losses: 1, winRatePct: 0, totalPnl: -39.3, avgReturnPct: -6.71, avgHoldingDays: 1 },
    ],
    exitQuality: { evaluated: 2, lockedGain: 1, soldEarly: 0, avoidedLoss: 1, soldLow: 0, breakeven: 0 },
    topWinners: [winner],
    topLosers: [loser],
    items: [winner, loser],
    notes: ['已平仓仅 2 笔（< 30 笔），结论仅供方向性参考'],
    ...overrides,
  };
}

function renderPanel() {
  return render(
    <UiLanguageProvider>
      <PortfolioSellReviewPanel accountId={42} />
    </UiLanguageProvider>,
  );
}

describe('PortfolioSellReviewPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
    getSellReview.mockResolvedValue(makeReview());
  });

  it('renders summary, exit-reason stats and attribution', async () => {
    renderPanel();

    await waitFor(() => expect(screen.getByText('卖出复盘')).toBeTruthy());
    expect(screen.getByText('CNY 750.88')).toBeTruthy();
    expect(screen.getByText('50.00%')).toBeTruthy();
    expect(screen.getByText('按退出原因')).toBeTruthy();
    expect(screen.getAllByText('止盈触发').length).toBeGreaterThan(0);
    expect(screen.getAllByText('止损触发').length).toBeGreaterThan(0);
    expect(screen.getByText('最赚钱的几笔：为什么赚')).toBeTruthy();
    expect(screen.getByText('最亏钱的几笔：为什么亏')).toBeTruthy();
    expect(screen.getAllByText(/建仓理由: 业绩超预期叠加资金流入/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/落袋正确（卖后回落）/).length).toBeGreaterThan(0);
    expect(screen.getByText(/已检验 2\/2/)).toBeTruthy();
    expect(getSellReview).toHaveBeenCalledWith({ accountId: 42, asOf: undefined });
  });

  it('shows the pending hint when some trades lack post-sell bars', async () => {
    getSellReview.mockResolvedValue(
      makeReview({
        exitQuality: { evaluated: 1, lockedGain: 1, soldEarly: 0, avoidedLoss: 0, soldLow: 0, breakeven: 0 },
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByText(/已检验 1\/2/)).toBeTruthy());
    expect(screen.getByText('卖后日线不足，走势检验随数据积累逐步生效')).toBeTruthy();
  });

  it('reloads when the refresh button is clicked', async () => {
    renderPanel();

    await waitFor(() => expect(getSellReview).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(getSellReview).toHaveBeenCalledTimes(2));
  });

  it('shows an alert when the request fails', async () => {
    getSellReview.mockRejectedValue(new Error('boom'));
    renderPanel();

    await waitFor(() => expect(screen.getByText('卖出复盘加载失败')).toBeTruthy());
    expect(screen.getByText('boom')).toBeTruthy();
  });

  it('shows the empty state when there are no closed trades', async () => {
    getSellReview.mockResolvedValue(
      makeReview({
        summary: { count: 0, wins: 0, losses: 0, winRatePct: null, realizedPnl: 0, avgReturnPct: null, avgHoldingDays: null },
        byReason: [],
        exitQuality: { evaluated: 0 },
        topWinners: [],
        topLosers: [],
        items: [],
        notes: ['尚无已平仓交易，卖出复盘为空'],
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByText('尚无已平仓交易')).toBeTruthy());
  });
});