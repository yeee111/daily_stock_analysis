import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Clock3, ListChecks, RefreshCw, Server, TimerReset } from 'lucide-react';
import { runtimeApi, type RuntimeJob, type RuntimeOverview } from '../api/runtime';
import { systemConfigApi } from '../api/systemConfig';
import type { ParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  PageHeader,
  StatCard,
} from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey, UiTextParams } from '../i18n/uiText';
import { cn } from '../utils/cn';

type Translate = (key: UiTextKey, params?: UiTextParams) => string;
type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'history';

const POLL_INTERVAL_MS = 10000;

const JOB_LABEL_KEYS: Record<string, UiTextKey> = {
  analysis: 'runtime.job.analysis',
  paper_trading: 'runtime.job.paperTrading',
  agent_event_monitor: 'runtime.job.eventMonitor',
};

const SERVICE_LABEL_KEYS: Record<string, UiTextKey> = {
  backend: 'runtime.service.backend',
  scheduler: 'runtime.service.scheduler',
  event_monitor: 'runtime.service.eventMonitor',
  task_queue: 'runtime.service.taskQueue',
  paper_trading: 'runtime.service.paperTrading',
};

const STATUS_LABEL_KEYS: Record<string, UiTextKey> = {
  running: 'runtime.status.running',
  busy: 'runtime.status.busy',
  idle: 'runtime.status.idle',
  scheduled: 'runtime.status.scheduled',
  disabled: 'runtime.status.disabled',
  stopped: 'runtime.status.stopped',
  pending: 'runtime.taskStatus.pending',
  processing: 'runtime.taskStatus.processing',
  completed: 'runtime.taskStatus.completed',
  failed: 'runtime.taskStatus.failed',
  cancelled: 'runtime.taskStatus.cancelled',
  cancel_requested: 'runtime.taskStatus.cancelRequested',
};

const STATUS_BADGE_VARIANTS: Record<string, BadgeVariant> = {
  running: 'success',
  busy: 'info',
  scheduled: 'info',
  idle: 'default',
  disabled: 'default',
  stopped: 'danger',
  pending: 'default',
  processing: 'info',
  completed: 'success',
  failed: 'danger',
  cancelled: 'default',
};

function getLocale(language: UiLanguage): string {
  return language === 'en' ? 'en-US' : 'zh-CN';
}

function formatDateTime(value: string | null | undefined, language: UiLanguage): string {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(getLocale(language), {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function formatDuration(seconds: number | null | undefined, t: Translate): string {
  if (seconds === null || seconds === undefined || seconds < 0) {
    return '-';
  }
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;
  const parts: string[] = [];
  if (hours > 0) {
    parts.push(t('runtime.duration.hours', { hours }));
  }
  if (minutes > 0) {
    parts.push(t('runtime.duration.minutes', { minutes }));
  }
  if (hours === 0 && minutes === 0) {
    parts.push(t('runtime.duration.seconds', { seconds: remaining }));
  }
  return parts.join(' ');
}

function getJobLabel(job: RuntimeJob, t: Translate): string {
  const key = JOB_LABEL_KEYS[job.id];
  return key ? t(key) : job.id;
}

function getServiceLabel(id: string, t: Translate): string {
  const key = SERVICE_LABEL_KEYS[id];
  return key ? t(key) : id;
}

function getStatusLabel(status: string, t: Translate): string {
  const key = STATUS_LABEL_KEYS[status];
  return key ? t(key) : status;
}

function buildParsedError(error: unknown, t: Translate): ParsedApiError {
  if (error && typeof error === 'object' && 'parsedError' in error) {
    const parsedError = (error as { parsedError?: ParsedApiError }).parsedError;
    if (parsedError) {
      return parsedError;
    }
  }
  const message = error instanceof Error ? error.message : t('runtime.error.message');
  return {
    title: t('runtime.error.title'),
    message,
    rawMessage: message,
    category: 'http_error',
  };
}

const RuntimePage: React.FC = () => {
  const { language, t } = useUiLanguage();
  const [overview, setOverview] = useState<RuntimeOverview | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [runningNow, setRunningNow] = useState(false);
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const requestSeqRef = useRef(0);

  const loadOverview = useCallback(async (silent = false) => {
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    if (!silent) {
      setLoading(true);
    }
    try {
      const data = await runtimeApi.getOverview();
      if (requestSeq !== requestSeqRef.current) {
        return;
      }
      setOverview(data);
      setError(null);
    } catch (err) {
      if (requestSeq !== requestSeqRef.current) {
        return;
      }
      setError(buildParsedError(err, t));
    } finally {
      if (requestSeq === requestSeqRef.current && !silent) {
        setLoading(false);
      }
    }
  }, [t]);

  useEffect(() => {
    void loadOverview();
    return () => {
      requestSeqRef.current += 1;
    };
  }, [loadOverview]);

  useEffect(() => {
    if (!autoRefresh) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      void loadOverview(true);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadOverview]);

  const handleRunNow = useCallback(async () => {
    setRunningNow(true);
    setRunMessage(null);
    try {
      await systemConfigApi.runSchedulerNow();
      setRunMessage(t('runtime.jobs.runAccepted'));
      void loadOverview(true);
    } catch (err) {
      const parsed = buildParsedError(err, t);
      setRunMessage(parsed.message);
    } finally {
      setRunningNow(false);
    }
  }, [loadOverview, t]);

  const scheduler = overview?.scheduler;
  const processInfo = overview?.process;
  const taskQueue = overview?.taskQueue;
  const eventMonitor = overview?.eventMonitor;
  const jobs = scheduler?.jobs ?? [];

  const describeService = useCallback((serviceId: string): string => {
    if (!overview) {
      return '-';
    }
    const host = overview.process.host ?? '127.0.0.1';
    const port = overview.process.port ?? '-';
    switch (serviceId) {
      case 'backend':
        return t('runtime.serviceDetail.backend', {
          address: `${host}:${port}`,
          pid: overview.process.pid,
          duration: formatDuration(overview.process.uptimeSeconds, t),
        });
      case 'scheduler':
        return overview.scheduler.enabled
          ? t('runtime.serviceDetail.scheduler', {
            times: (overview.scheduler.scheduleTimes ?? []).join(' / ') || '-',
            next: formatDateTime(overview.scheduler.nextRunAt, language),
          })
          : t('runtime.stat.schedulerDisabled');
      case 'event_monitor':
        return overview.eventMonitor.enabled
          ? t('runtime.stat.eventMonitorHint', { minutes: overview.eventMonitor.intervalMinutes ?? '-' })
          : t('runtime.stat.eventMonitorDisabled');
      case 'task_queue':
        return t('runtime.serviceDetail.taskQueue', {
          processing: overview.taskQueue.stats.processing ?? 0,
          pending: overview.taskQueue.stats.pending ?? 0,
          max: overview.taskQueue.maxWorkers ?? 0,
        });
      case 'paper_trading':
        return overview.paperTrading.scheduled
          ? t('runtime.serviceDetail.paperTrading', {
            times: overview.paperTrading.scheduleTimes.join(' / ') || '-',
            autoRun: overview.paperTrading.autoRun ? t('common.enabled') : t('common.disabled'),
          })
          : t('runtime.status.disabled');
      default:
        return '-';
    }
  }, [language, overview, t]);

  return (
    <AppPage>
      <div className="space-y-5">
        <PageHeader
          eyebrow={t('runtime.eyebrow')}
          title={t('runtime.title')}
          description={t('runtime.description')}
          actions={(
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                aria-pressed={autoRefresh}
                onClick={() => setAutoRefresh((value) => !value)}
                className={cn(
                  'rounded-xl border px-3 py-2 text-sm transition-colors',
                  autoRefresh
                    ? 'border-cyan/30 bg-cyan/10 text-cyan'
                    : 'border-border/70 bg-card text-secondary-text hover:bg-hover hover:text-foreground'
                )}
              >
                {autoRefresh ? t('runtime.autoRefreshOn') : t('runtime.autoRefreshOff')}
              </button>
              <button
                type="button"
                className="btn-secondary inline-flex items-center gap-2"
                onClick={() => void loadOverview()}
                disabled={loading}
              >
                <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
                {t('runtime.refresh')}
              </button>
            </div>
          )}
        />

        {error ? (
          <ApiErrorAlert error={error} actionLabel={t('common.retry')} onAction={() => void loadOverview()} />
        ) : null}

        {scheduler?.lastError ? (
          <InlineAlert
            variant="danger"
            title={t('runtime.alert.lastError')}
            message={scheduler.lastError}
          />
        ) : null}

        {runMessage ? (
          <InlineAlert variant="info" message={runMessage} />
        ) : null}

        {loading && !overview ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="h-28 animate-pulse rounded-2xl border border-border/70 bg-card/60" />
            ))}
          </div>
        ) : null}

        {overview && processInfo && scheduler && taskQueue && eventMonitor ? (
          <>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <StatCard
                label={t('runtime.stat.backend')}
                value={getStatusLabel('running', t)}
                hint={t('runtime.stat.backendHint', {
                  address: `${processInfo.host ?? '127.0.0.1'}:${processInfo.port ?? '-'}`,
                  pid: processInfo.pid,
                  duration: formatDuration(processInfo.uptimeSeconds, t),
                })}
                icon={<Server className="h-5 w-5" />}
                tone="success"
              />
              <StatCard
                label={t('runtime.stat.scheduler')}
                value={scheduler.enabled ? t('runtime.stat.schedulerEnabled') : t('runtime.stat.schedulerDisabled')}
                hint={scheduler.nextRunAt
                  ? t('runtime.stat.schedulerHint', { next: formatDateTime(scheduler.nextRunAt, language) })
                  : t('runtime.stat.noNextRun')}
                icon={<Clock3 className="h-5 w-5" />}
                tone={scheduler.enabled ? 'primary' : 'default'}
              />
              <StatCard
                label={t('runtime.stat.queue')}
                value={`${taskQueue.stats.processing ?? 0} / ${taskQueue.maxWorkers ?? 0}`}
                hint={t('runtime.stat.queueHint', {
                  processing: taskQueue.stats.processing ?? 0,
                  pending: taskQueue.stats.pending ?? 0,
                  max: taskQueue.maxWorkers ?? 0,
                })}
                icon={<ListChecks className="h-5 w-5" />}
                tone={(taskQueue.stats.processing ?? 0) > 0 ? 'primary' : 'default'}
              />
              <StatCard
                label={t('runtime.stat.eventMonitor')}
                value={eventMonitor.enabled
                  ? (eventMonitor.active ? t('runtime.status.running') : t('runtime.status.idle'))
                  : t('runtime.status.disabled')}
                hint={eventMonitor.enabled
                  ? t('runtime.stat.eventMonitorHint', { minutes: eventMonitor.intervalMinutes ?? '-' })
                  : t('runtime.stat.eventMonitorDisabled')}
                icon={<Activity className="h-5 w-5" />}
                tone={eventMonitor.active ? 'success' : 'default'}
              />
            </div>

            <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
              <Card title={t('runtime.services.title')} subtitle={t('runtime.services.description')} padding="none">
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-border/70 text-sm">
                    <thead className="bg-surface-2/70 text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
                      <tr>
                        <th className="px-4 py-3 font-medium">{t('runtime.services.name')}</th>
                        <th className="px-4 py-3 font-medium">{t('runtime.services.status')}</th>
                        <th className="px-4 py-3 font-medium">{t('runtime.services.detail')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {overview.services.map((service) => (
                        <tr key={service.id} className="hover:bg-hover/60">
                          <td className="whitespace-nowrap px-4 py-3 font-medium text-foreground">
                            {getServiceLabel(service.id, t)}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            <Badge variant={STATUS_BADGE_VARIANTS[service.status] ?? 'default'}>
                              {getStatusLabel(service.status, t)}
                            </Badge>
                          </td>
                          <td className="px-4 py-3 text-secondary-text">{describeService(service.id)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>

              <Card title={t('runtime.process.title')} padding="none">
                <dl className="divide-y divide-border/60 text-sm">
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.pid')}</dt>
                    <dd className="font-medium text-foreground">{processInfo.pid}</dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.address')}</dt>
                    <dd className="font-medium text-foreground">
                      {`${processInfo.host ?? '127.0.0.1'}:${processInfo.port ?? '-'}`}
                    </dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.startedAt')}</dt>
                    <dd className="font-medium text-foreground">{formatDateTime(processInfo.startedAt, language)}</dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.uptime')}</dt>
                    <dd className="font-medium text-foreground">{formatDuration(processInfo.uptimeSeconds, t)}</dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.python')}</dt>
                    <dd className="font-medium text-foreground">{processInfo.pythonVersion ?? '-'}</dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.mode')}</dt>
                    <dd className="font-medium text-foreground">
                      {processInfo.desktopMode
                        ? t('runtime.process.modeDesktop')
                        : t('runtime.process.modeStandalone')}
                    </dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="shrink-0 text-secondary-text">{t('runtime.process.cwd')}</dt>
                    <dd className="truncate text-right font-medium text-foreground" title={processInfo.cwd ?? ''}>
                      {processInfo.cwd ?? '-'}
                    </dd>
                  </div>
                  <div className="flex items-start justify-between gap-4 px-4 py-3">
                    <dt className="text-secondary-text">{t('runtime.process.build')}</dt>
                    <dd className="font-medium text-foreground">{processInfo.build?.version ?? '-'}</dd>
                  </div>
                </dl>
              </Card>
            </div>

            <Card
              title={t('runtime.jobs.title')}
              subtitle={t('runtime.jobs.description')}
              padding="none"
            >
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-border/70 text-sm">
                  <thead className="bg-surface-2/70 text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
                    <tr>
                      <th className="px-4 py-3 font-medium">{t('runtime.jobs.name')}</th>
                      <th className="px-4 py-3 font-medium">{t('runtime.jobs.times')}</th>
                      <th className="px-4 py-3 font-medium">{t('runtime.jobs.nextRun')}</th>
                      <th className="px-4 py-3 font-medium">{t('runtime.jobs.lastRun')}</th>
                      <th className="px-4 py-3 text-right font-medium">{t('runtime.jobs.action')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/60">
                    {jobs.length === 0 ? (
                      <tr>
                        <td colSpan={5} className="px-4 py-8 text-center text-secondary-text">
                          {t('runtime.jobs.empty')}
                        </td>
                      </tr>
                    ) : jobs.map((job) => {
                      const isAnalysis = job.id === 'analysis';
                      const jobStatus = isAnalysis
                        ? (job.running ? 'running' : 'scheduled')
                        : job.kind === 'interval'
                          ? (job.running ? 'running' : 'idle')
                          : 'scheduled';
                      return (
                        <tr key={`${job.id}-${job.kind}`} className="hover:bg-hover/60">
                          <td className="px-4 py-3">
                            <div className="flex items-center gap-2 font-medium text-foreground">
                              <span>{getJobLabel(job, t)}</span>
                              <Badge variant={STATUS_BADGE_VARIANTS[jobStatus] ?? 'default'}>
                                {getStatusLabel(jobStatus, t)}
                              </Badge>
                            </div>
                          </td>
                          <td className="px-4 py-3 text-secondary-text">
                            {job.kind === 'interval'
                              ? t('runtime.job.interval', { seconds: job.intervalSeconds ?? '-' })
                              : (job.scheduleTimes ?? []).join(' / ') || '-'}
                          </td>
                          <td className="px-4 py-3 text-secondary-text">
                            {formatDateTime(job.nextRunAt, language)}
                          </td>
                          <td className="px-4 py-3 text-secondary-text">
                            {formatDateTime(
                              isAnalysis ? scheduler.lastRunAt : job.lastRunAt,
                              language,
                            )}
                          </td>
                          <td className="px-4 py-3 text-right">
                            {isAnalysis ? (
                              <Button
                                size="sm"
                                variant="outline"
                                isLoading={runningNow}
                                loadingText={t('runtime.jobs.runningNow')}
                                onClick={() => void handleRunNow()}
                              >
                                <TimerReset className="h-4 w-4" />
                                {t('runtime.jobs.runNow')}
                              </Button>
                            ) : (
                              <span className="text-secondary-text">-</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card title={t('runtime.queue.title')} subtitle={t('runtime.queue.description')} padding="none">
              {taskQueue.recentTasks.length === 0 ? (
                <div className="p-4">
                  <EmptyState
                    title={t('runtime.queue.empty')}
                    description={t('runtime.queue.emptyDescription')}
                  />
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-border/70 text-sm">
                    <thead className="bg-surface-2/70 text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
                      <tr>
                        <th className="px-4 py-3 font-medium">{t('runtime.queue.stock')}</th>
                        <th className="px-4 py-3 font-medium">{t('runtime.queue.status')}</th>
                        <th className="px-4 py-3 font-medium">{t('runtime.queue.progress')}</th>
                        <th className="px-4 py-3 font-medium">{t('runtime.queue.createdAt')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {taskQueue.recentTasks.map((task) => (
                        <tr key={task.taskId} className="hover:bg-hover/60">
                          <td className="px-4 py-3">
                            <div className="font-medium text-foreground">{task.stockName || task.stockCode}</div>
                            <div className="text-xs text-secondary-text">{task.stockCode}</div>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            <Badge variant={STATUS_BADGE_VARIANTS[task.status] ?? 'default'}>
                              {getStatusLabel(task.status, t)}
                            </Badge>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-secondary-text">{task.progress}%</td>
                          <td className="whitespace-nowrap px-4 py-3 text-secondary-text">
                            {formatDateTime(task.createdAt, language)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <p className="text-right text-xs text-secondary-text">
              {t('runtime.updatedAt', { time: formatDateTime(overview.generatedAt, language) })}
            </p>
          </>
        ) : null}
      </div>
    </AppPage>
  );
};

export default RuntimePage;
