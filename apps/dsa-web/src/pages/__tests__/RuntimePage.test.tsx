import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeOverview } from '../../api/runtime';
import RuntimePage from '../RuntimePage';

const { getOverview } = vi.hoisted(() => ({ getOverview: vi.fn() }));
const { runSchedulerNow } = vi.hoisted(() => ({ runSchedulerNow: vi.fn() }));

vi.mock('../../api/runtime', () => ({
  runtimeApi: { getOverview: () => getOverview() },
}));

vi.mock('../../api/systemConfig', () => ({
  systemConfigApi: { runSchedulerNow: () => runSchedulerNow() },
}));

const overview: RuntimeOverview = {
  generatedAt: '2026-11-20T22:00:00',
  process: {
    pid: 2600,
    startedAt: '2026-11-20T18:00:00',
    uptimeSeconds: 7200,
    pythonVersion: '3.12.10',
    executable: 'python.exe',
    cwd: 'D:\\qoder-D\\qoder\\daily_stock_analysis',
    argv: ['main.py', '--webui-only'],
    host: '127.0.0.1',
    port: 8000,
    desktopMode: false,
    build: { version: '745ff8d-dirty', revision: '745ff8d', builtAt: null },
  },
  scheduler: {
    enabled: true,
    running: false,
    scheduleTimes: ['18:00'],
    paperTradingScheduleTimes: ['18:05', '19:00'],
    nextRunAt: '2026-11-21T18:00:00',
    lastRunAt: '2026-11-20T18:01:00',
    lastSuccessAt: '2026-11-20T18:01:00',
    lastError: null,
    lastSkippedAt: null,
    lastSkipReason: null,
    jobs: [
      {
        id: 'analysis',
        kind: 'daily',
        scheduleTimes: ['18:00'],
        nextRunAt: '2026-11-21T18:00:00',
        running: false,
      },
      {
        id: 'paper_trading',
        kind: 'daily',
        scheduleTimes: ['18:05', '19:00'],
        nextRunAt: '2026-11-21T18:05:00',
      },
      {
        id: 'agent_event_monitor',
        kind: 'interval',
        intervalSeconds: 300,
        lastRunAt: '2026-11-20T21:55:00',
        running: true,
      },
    ],
  },
  taskQueue: {
    maxWorkers: 3,
    stats: { total: 1, pending: 0, processing: 0, completed: 1, failed: 0 },
    recentTasks: [{
      taskId: 'task-1',
      stockCode: '600519',
      stockName: '贵州茅台',
      status: 'completed',
      progress: 100,
      createdAt: '2026-11-20T18:00:00',
    }],
  },
  eventMonitor: { enabled: true, active: true, intervalMinutes: 5 },
  paperTrading: { scheduled: true, autoRun: true, scheduleTimes: ['18:05', '19:00'] },
  services: [
    { id: 'backend', status: 'running', detail: {} },
    { id: 'scheduler', status: 'running', detail: {} },
    { id: 'event_monitor', status: 'running', detail: {} },
    { id: 'task_queue', status: 'idle', detail: {} },
    { id: 'paper_trading', status: 'scheduled', detail: {} },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  getOverview.mockResolvedValue(overview);
  runSchedulerNow.mockResolvedValue({ accepted: true, running: true });
});

describe('RuntimePage', () => {
  it('renders the running services and scheduled jobs', async () => {
    render(<MemoryRouter><RuntimePage /></MemoryRouter>);

    expect(await screen.findByRole('heading', { name: '运行状态' })).toBeInTheDocument();
    expect(getOverview).toHaveBeenCalledTimes(1);
    expect(screen.getByText('定时分析调度')).toBeInTheDocument();
    expect(screen.getByText('告警事件监控')).toBeInTheDocument();
    expect(screen.getByText('纸面交易定时任务')).toBeInTheDocument();
    expect(screen.getByText('每日分析')).toBeInTheDocument();
    expect(screen.getByText('每 300 秒')).toBeInTheDocument();
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('127.0.0.1:8000')).toBeInTheDocument();
  });

  it('submits a run-now request from the analysis job row', async () => {
    render(<MemoryRouter><RuntimePage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole('button', { name: '立即执行' }));

    await waitFor(() => expect(runSchedulerNow).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('已提交执行请求。')).toBeInTheDocument();
  });

  it('shows the last scheduler error', async () => {
    getOverview.mockResolvedValue({
      ...overview,
      scheduler: { ...overview.scheduler, lastError: 'analysis failed' },
    });

    render(<MemoryRouter><RuntimePage /></MemoryRouter>);

    expect(await screen.findByText('analysis failed')).toBeInTheDocument();
  });

  it('shows a load error and can retry', async () => {
    getOverview.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(overview);

    render(<MemoryRouter><RuntimePage /></MemoryRouter>);

    expect(await screen.findByText('运行状态加载失败')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.getByText('定时分析调度')).toBeInTheDocument());
  });
});
