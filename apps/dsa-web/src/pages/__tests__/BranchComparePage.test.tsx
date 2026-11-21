// 分支对比页渲染验证：mock 只读对比接口，覆盖 空态 / 有数据 / 加载失败 三态。
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import BranchComparePage from '../BranchComparePage';
import { paperTradingApi } from '../../api/paperTrading';
import type { BranchComparisonResponse } from '../../types/paperBranchCompare';

vi.mock('../../api/paperTrading', () => ({
  paperTradingApi: { comparison: vi.fn() },
}));

const mockedComparison = vi.mocked(paperTradingApi.comparison);

const emptyResponse: BranchComparisonResponse = {
  asOf: '2026-11-17',
  costMethod: 'fifo',
  lookbackDays: 180,
  thresholds: {},
  window: { start: null, end: null, days: 0, referenceAccountId: null },
  branches: [],
  warnings: [],
};

const dataResponse: BranchComparisonResponse = {
  ...emptyResponse,
  window: { start: '2026-11-18', end: '2026-11-25', days: 6, referenceAccountId: 2 },
  branches: [
    {
      accountId: 2, accountName: '纸面交易', label: null, role: 'live', state: 'live',
      parentAccountId: null, forkDate: null, profileId: 1, profileName: 'default',
      profileVersion: 1, autoRun: true, isReference: true,
      series: [{ date: '2026-11-18', nav: 100 }, { date: '2026-11-19', nav: 100.4 }],
      metrics: { days: 6, returnPct: 0.4, maxDrawdownPct: -0.3, trades: 4, excessPct: 0, excess: 0, rollingWin: null, trackingDev: 0, topDays: 0, referenceMaxDrawdownPct: -0.3 },
      gates: [], passed: null, score: null,
    },
    {
      accountId: 3, accountName: '分支A-仅RR1.0', label: 'A=仅RR@1.0', role: 'control', state: 'shadow',
      parentAccountId: 2, forkDate: '2026-11-18', profileId: 2, profileName: 'rr10',
      profileVersion: 1, autoRun: true, isReference: false,
      series: [{ date: '2026-11-18', nav: 100 }, { date: '2026-11-19', nav: 100.9 }],
      metrics: { days: 6, returnPct: 0.9, maxDrawdownPct: -0.5, trades: 3, excessPct: 0.5, excess: 0.005, rollingWin: 0.67, trackingDev: 0.2, topDays: 2, referenceMaxDrawdownPct: -0.3 },
      gates: [
        { name: '可比天数', value: 6, threshold: '≥20 日', ok: false, status: 'fail', hint: '共同窗口交易日数' },
        { name: '成交笔数', value: 3, threshold: '≥8 笔', ok: false, status: 'fail', hint: '买/卖各计 1 笔' },
        { name: '参数稳健性', value: null, threshold: 'na', ok: null, status: 'na', hint: 'DSA 分支不适用' },
      ],
      passed: false, score: 41.5,
    },
  ],
};

describe('BranchComparePage', () => {
  beforeEach(() => {
    mockedComparison.mockReset();
  });

  it('空数据（还没 fork）显示友好空态与纪律提示，不报错', async () => {
    mockedComparison.mockResolvedValue(emptyResponse);
    render(<BranchComparePage />);
    expect(await screen.findByText('还没有可对比的风控分支')).toBeTruthy();
    expect(screen.getByText(/30\+ 笔平仓前不下结论/)).toBeTruthy();
    expect(screen.queryByText(/加载失败/)).toBeNull();
  });

  it('有数据时渲染分支汇总卡、门槛表与中性状态标注', async () => {
    mockedComparison.mockResolvedValue(dataResponse);
    render(<BranchComparePage />);
    expect(await screen.findByText('A=仅RR@1.0')).toBeTruthy();
    expect(screen.getByText('转正门槛 · A=仅RR@1.0')).toBeTruthy();
    expect(screen.getByText('可比天数')).toBeTruthy();
    expect(screen.getAllByText('未达').length).toBeGreaterThan(0);
    expect(screen.getByText('不适用')).toBeTruthy();
    expect(screen.getByText(/暂无数据源/)).toBeTruthy();
    expect(screen.getByText(/30\+ 笔平仓前不下结论/)).toBeTruthy();
  });

  it('接口失败显示错误提示而非崩溃', async () => {
    mockedComparison.mockRejectedValue(new Error('network down'));
    render(<BranchComparePage />);
    expect(await screen.findByText('对比数据加载失败')).toBeTruthy();
    expect(screen.getByText(/network down/)).toBeTruthy();
  });

  it('只调用只读 comparison 接口，不触发 run 等写操作', async () => {
    mockedComparison.mockResolvedValue(emptyResponse);
    render(<BranchComparePage />);
    await waitFor(() => expect(mockedComparison).toHaveBeenCalledTimes(1));
    expect(Object.keys(paperTradingApi)).toEqual(['comparison']);
  });
});