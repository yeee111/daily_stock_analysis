// 回归：克隆账户历史跨账户重复时，React key 必须唯一，
// 否则从"全部账户"切到单账户后旧行纤维无法被 reconciliation 移除（DOM 残留重复行）。
// 数据本身不去重——全部账户视图仍按接口返回行数渲染。
import { render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { PortfolioSellReviewPanel } from '../PortfolioSellReviewPanel';

const { getSellReview } = vi.hoisted(() => ({ getSellReview: vi.fn() }));

vi.mock('../../../api/portfolio', () => ({
  portfolioApi: { getSellReview },
}));

function makeItem(code: string, closeDate: string) {
  return {
    stockCode: code,
    stockName: '牧原股份',
    closeDate,
    openDate: '2026-11-16',
    exitReason: 'take_profit',
    exitReasonLabel: '止盈触发',
    holdingDays: 1,
    quantity: 200,
    returnPct: 0.06,
    pnl: 5.22,
    postSell5dPct: null,
    postSell10dPct: null,
    postSell20dPct: null,
    exitVerdict: null,
    exitVerdictLabel: null,
    verdictHorizon: null,
    entryScore: 60,
    entryReason: null,
  };
}

function makeResponse(items: ReturnType<typeof makeItem>[]) {
  return {
    asOf: '2026-11-18',
    accountId: null,
    summary: { count: items.length, winRatePct: 100, realizedPnl: 15.66, avgReturnPct: 0.06, avgHoldingDays: 1 },
    byReason: [],
    exitQuality: { evaluated: 0, lockedGain: 0, soldEarly: 0, avoidedLoss: 0, soldLow: 0 },
    topWinners: [],
    topLosers: [],
    items,
    notes: [],
  };
}

const rowCount = () => document.querySelectorAll('tbody tr').length;

describe('PortfolioSellReviewPanel key 唯一性回归', () => {
  beforeEach(() => {
    getSellReview.mockReset();
  });

  it('全部账户视图：同代码同平仓日的克隆历史按返回行数渲染（不去重）', async () => {
    getSellReview.mockResolvedValue(
      makeResponse([
        makeItem('002714', '2026-11-17'),
        makeItem('002714', '2026-11-17'),
        makeItem('002714', '2026-11-17'),
      ]),
    );
    render(
      <UiLanguageProvider>
        <PortfolioSellReviewPanel accountId={null} />
      </UiLanguageProvider>,
    );
    await waitFor(() => expect(rowCount()).toBe(3));
  });

  it('全部账户切到单账户后明细行不残留（重复 key 回归）', async () => {
    getSellReview.mockResolvedValueOnce(
      makeResponse([
        makeItem('002714', '2026-11-17'),
        makeItem('002714', '2026-11-17'),
        makeItem('002714', '2026-11-17'),
      ]),
    );
    const { rerender } = render(
      <UiLanguageProvider>
        <PortfolioSellReviewPanel accountId={null} />
      </UiLanguageProvider>,
    );
    await waitFor(() => expect(rowCount()).toBe(3));

    getSellReview.mockResolvedValueOnce(makeResponse([makeItem('002714', '2026-11-17')]));
    rerender(
      <UiLanguageProvider>
        <PortfolioSellReviewPanel accountId={3} />
      </UiLanguageProvider>,
    );
    await waitFor(() => expect(rowCount()).toBe(1));
    expect(getSellReview).toHaveBeenLastCalledWith({ accountId: 3, asOf: undefined });
  });
});