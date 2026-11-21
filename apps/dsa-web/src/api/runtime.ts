import apiClient from './index';
import { toCamelCase } from './utils';

export type RuntimeServiceStatus = 'running' | 'idle' | 'busy' | 'scheduled' | 'disabled' | 'stopped';

export type RuntimeService = {
  id: string;
  status: RuntimeServiceStatus;
  detail: Record<string, unknown>;
};

export type RuntimeJob = {
  id: string;
  kind: string;
  scheduleTimes?: string[] | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  intervalSeconds?: number | null;
  running?: boolean | null;
};

export type RuntimeProcess = {
  pid: number;
  startedAt?: string | null;
  uptimeSeconds?: number | null;
  pythonVersion?: string | null;
  executable?: string | null;
  cwd?: string | null;
  argv?: string[];
  host?: string | null;
  port?: number | null;
  desktopMode?: boolean;
  build?: {
    version?: string | null;
    revision?: string | null;
    builtAt?: string | null;
  };
};

export type RuntimeSchedulerStatus = {
  enabled: boolean;
  running: boolean;
  scheduleTimes: string[];
  paperTradingScheduleTimes: string[];
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastSuccessAt?: string | null;
  lastError?: string | null;
  lastSkippedAt?: string | null;
  lastSkipReason?: string | null;
  jobs: RuntimeJob[];
};

export type RuntimeTaskRecord = {
  taskId: string;
  stockCode: string;
  stockName?: string | null;
  status: string;
  progress: number;
  createdAt: string;
  completedAt?: string | null;
};

export type RuntimeTaskQueue = {
  maxWorkers: number;
  stats: {
    total?: number;
    pending?: number;
    processing?: number;
    completed?: number;
    failed?: number;
  };
  recentTasks: RuntimeTaskRecord[];
};

export type RuntimeEventMonitor = {
  enabled: boolean;
  active: boolean;
  intervalMinutes?: number | null;
};

export type RuntimePaperTrading = {
  scheduled: boolean;
  autoRun: boolean;
  scheduleTimes: string[];
};

export type RuntimeOverview = {
  generatedAt: string;
  process: RuntimeProcess;
  scheduler: RuntimeSchedulerStatus;
  taskQueue: RuntimeTaskQueue;
  eventMonitor: RuntimeEventMonitor;
  paperTrading: RuntimePaperTrading;
  services: RuntimeService[];
};

export const runtimeApi = {
  getOverview: async (): Promise<RuntimeOverview> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/runtime/overview');
    return toCamelCase<RuntimeOverview>(response.data);
  },
};
