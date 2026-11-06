import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { TrendingUp } from 'lucide-react';
import { portfolioApi } from '../../api/portfolio';
import { getParsedApiError } from '../../api/error';
import { Badge, Card, EmptyState, InlineAlert } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PORTFOLIO_PERFORMANCE_TEXT } from '../../locales/featureText';
import { formatMoney, formatPct, formatSignedPct } from '../../utils/portfolioFormat';
import type { PortfolioCostMethod, PortfolioPerformanceResponse } from '../../types/portfolio';

type PerformancePanelProps = {
  accountId?: number | null;
  asOf?: string | null;
  costMethod: PortfolioCostMethod;
  includeRealtime?: boolean;
};

type CurveDatum = {
  date: string;
  equity: number;
  portfolioReturn: number | null;
  benchmarkReturn: number | null;
  drawdown: number;
};

type CurveTooltipProps = {
  active?: boolean;
  payload?: Array<{ payload?: CurveDatum }>;
};

const CurveTooltip: React.FC<CurveTooltipProps> = ({ active, payload }) => {
  const { language } = useUiLanguage();
  const text = PORTFOLIO_PERFORMANCE_TEXT[language];
  if (!active || !payload?.[0]?.payload) return null;
  const datum = payload[0].payload;
  return (
    <div className="rounded-xl border border-border/70 bg-card/95 px-3 py-2 text-xs shadow-card">
      <div className="font-semibold text-foreground">{datum.date}</div>
      <div className="mt-2 grid gap-1 text-secondary-text">
        <span>{text.totalEquity}: {formatMoney(datum.equity)}</span>
        <span>{text.equity}: {formatSignedPct(datum.portfolioReturn)}</span>
        <span>{text.benchmark}: {datum.benchmarkReturn == null ? text.none : formatSignedPct(datum.benchmarkReturn)}</span>
        <span>{text.drawdown}: {formatSignedPct(datum.drawdown)}</span>
      </div>
    </div>
  );
};

export const PortfolioPerformancePanel: React.FC<PerformancePanelProps> = ({
  accountId = null,
  asOf = null,
  costMethod,
  includeRealtime = false,
}) => {
  const { language } = useUiLanguage();
  const text = PORTFOLIO_PERFORMANCE_TEXT[language];
  const [report, setReport] = useState<PortfolioPerformanceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [syncingQuotes, setSyncingQuotes] = useState(false);
  const [quotesNotice, setQuotesNotice] = useState<string | null>(null);
  const [quotesError, setQuotesError] = useState<string | null>(null);
  const [useRealtime, setUseRealtime] = useState(includeRealtime);

  const loadPerformance = useCallback(async (options?: { realtime?: boolean }) => {
    const realtime = options?.realtime ?? useRealtime;
    setLoading(true);
    setError(null);
    try {
      const data = await portfolioApi.getPerformance({
        accountId: accountId ?? undefined,
        asOf: asOf ?? undefined,
        costMethod,
        includeRealtime: realtime,
      });
      setReport(data);
    } catch (err) {
      const parsed = getParsedApiError(err);
      setError(parsed.message);
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [accountId, asOf, costMethod, useRealtime]);

  useEffect(() => {
    void loadPerformance();
  }, [loadPerformance]);

  const refreshQuotes = useCallback(async () => {
    setSyncingQuotes(true);
    setQuotesNotice(null);
    setQuotesError(null);
    try {
      const result = await portfolioApi.refreshMarketData({
        accountId: accountId ?? undefined,
        asOf: asOf ?? undefined,
      });
      const parts: string[] = [text.quotesRealtime];
      if (result.positions.synced.length > 0) {
        parts.push(text.quotesSynced.replace('{count}', String(result.positions.synced.length)));
      }
      if (result.positions.failed.length > 0) {
        parts.push(text.quotesFailed.replace('{count}', String(result.positions.failed.length)));
      }
      if (result.benchmark) {
        parts.push(
          result.benchmark.error
            ? text.benchmarkSyncFailed.replace('{error}', result.benchmark.error)
            : result.benchmark.synced > 0
              ? text.benchmarkSynced
                .replace('{symbol}', result.benchmark.symbol)
                .replace('{count}', String(result.benchmark.synced))
              : text.benchmarkUpToDate.replace('{symbol}', result.benchmark.symbol),
        );
      }
      setQuotesNotice(parts.join(' · '));
      setUseRealtime(true);
      await loadPerformance({ realtime: true });
    } catch (err) {
      setQuotesError(getParsedApiError(err).message);
    } finally {
      setSyncingQuotes(false);
    }
  }, [accountId, asOf, loadPerformance, text]);

  const curveData = useMemo<CurveDatum[]>(() => {
    const points = report?.equityCurve?.points ?? [];
    return points.map((point) => ({
      date: point.date,
      equity: point.equity,
      portfolioReturn: point.returnPct ?? null,
      benchmarkReturn: point.benchmarkReturnPct ?? null,
      drawdown: -(point.drawdownPct ?? 0),
    }));
  }, [report]);

  const summary = report?.summary;
  const closed = report?.closedTrades;
  const openPositions = report?.openPositions;
  const benchmark = report?.benchmark;
  const hasBenchmark = Boolean(benchmark?.available);

  return (
    <Card padding="md">
      <div className="mb-3 flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <TrendingUp className="h-4 w-4 text-cyan" />
            {text.title}
          </h2>
          <p className="mt-1 text-xs text-secondary">{text.description}</p>
        </div>
        <div className="flex flex-wrap gap-2 self-start">
          <button
            type="button"
            className="btn-secondary text-sm"
            disabled={syncingQuotes || loading}
            onClick={() => void refreshQuotes()}
          >
            {syncingQuotes ? text.refreshingQuotes : text.refreshQuotes}
          </button>
          <button
            type="button"
            className="btn-secondary text-sm"
            disabled={loading}
            onClick={() => void loadPerformance()}
          >
            {loading ? text.refreshing : text.refresh}
          </button>
        </div>
      </div>

      {quotesError ? (
        <InlineAlert
          variant="danger"
          title={text.quotesError}
          message={quotesError}
          className="mb-3 rounded-xl px-3 py-2 text-xs shadow-none"
        />
      ) : null}
      {quotesNotice ? (
        <p className="mb-3 text-xs text-secondary">{quotesNotice}</p>
      ) : null}

      {error ? (
        <InlineAlert
          variant="danger"
          title={text.errorTitle}
          message={error}
          className="mb-3 rounded-xl px-3 py-2 text-xs shadow-none"
        />
      ) : null}

      {loading && report === null ? (
        <p className="text-xs text-secondary">{text.loading}</p>
      ) : null}

      {!loading && report === null && !error ? (
        <EmptyState
          className="border-none bg-transparent py-6 shadow-none"
          title={text.noCurve}
          description={text.description}
          icon={<TrendingUp className="h-6 w-6" />}
        />
      ) : null}

      {report && summary ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <MetricTile label={text.totalReturn} value={formatSignedPct(summary.totalReturnPct)} hint={formatMoney(summary.totalPnl)} tone={summary.totalPnl >= 0 ? 'up' : 'down'} />
            <MetricTile label={text.totalEquity} value={formatMoney(summary.totalEquity)} hint={`${text.netDeposits} ${formatMoney(summary.netDeposits)}`} />
            <MetricTile label={text.unrealizedPnl} value={formatMoney(summary.unrealizedPnl)} hint={`${text.realizedPnl} ${formatMoney(summary.realizedPnl)}`} tone={summary.unrealizedPnl >= 0 ? 'up' : 'down'} />
            <MetricTile label={text.maxDrawdown} value={formatPct(summary.maxDrawdownPct)} hint={`${text.currentDrawdown} ${formatPct(summary.currentDrawdownPct)}`} />
            <MetricTile label={text.winRate} value={closed?.winRatePct == null ? text.none : formatPct(closed.winRatePct)} hint={`${text.wins} ${closed?.wins ?? 0} / ${text.losses} ${closed?.losses ?? 0}`} />
            <MetricTile label={text.profitFactor} value={closed?.profitFactor == null ? text.none : closed.profitFactor.toFixed(2)} hint={`${text.avgReturn} ${closed?.avgReturnPct == null ? text.none : formatSignedPct(closed.avgReturnPct)}`} />
            <MetricTile label={text.avgHoldingDays} value={closed?.avgHoldingDays == null ? text.none : String(closed.avgHoldingDays)} hint={`${text.maxHoldingDays} ${closed?.maxHoldingDays ?? text.none}`} />
            <MetricTile label={text.investedRatio} value={formatPct(summary.investedRatioPct)} hint={`${text.positionCount} ${summary.positionCount} · ${text.tradeCount} ${summary.tradeCount}`} />
          </div>

          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
            <span>{text.firstActivity}: {summary.firstActivityDate ?? text.none}</span>
            <span>{text.daysActive}: {summary.daysActive ?? text.none}</span>
            <span>{text.feeTotal}: {formatMoney(summary.feeTotal + summary.taxTotal)}</span>
            <span>{text.benchmarkReturn}: {hasBenchmark ? formatSignedPct(benchmark?.returnPct) : text.none}</span>
            <span>{text.excessReturn}: {hasBenchmark ? formatSignedPct(benchmark?.excessReturnPct) : text.none}</span>
            {hasBenchmark && benchmark?.symbol ? <Badge size="sm">{text.benchmarkLabel.replace('{symbol}', benchmark.symbol)}</Badge> : null}
          </div>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold text-foreground">{text.curveTitle}</h3>
            {curveData.length === 0 ? (
              <p className="text-xs text-secondary">{text.noCurve}</p>
            ) : (
              <div className="h-[280px] min-h-[280px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={curveData} margin={{ top: 12, right: 16, bottom: 8, left: 4 }}>
                    <CartesianGrid stroke="rgba(148, 163, 184, 0.18)" vertical={false} />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 11, fill: 'currentColor' }}
                      stroke="rgba(148, 163, 184, 0.5)"
                      minTickGap={24}
                    />
                    <YAxis
                      tickFormatter={(value: number) => `${value.toFixed(1)}%`}
                      tick={{ fontSize: 11, fill: 'currentColor' }}
                      stroke="rgba(148, 163, 184, 0.5)"
                      width={56}
                    />
                    <Tooltip content={<CurveTooltip />} />
                    <Legend verticalAlign="bottom" height={28} wrapperStyle={{ fontSize: 12 }} />
                    <Line type="monotone" dataKey="portfolioReturn" name={text.equity} stroke="#00d4ff" strokeWidth={2} dot={false} />
                    {hasBenchmark ? (
                      <Line type="monotone" dataKey="benchmarkReturn" name={text.benchmark} stroke="#ffaa00" strokeWidth={2} dot={false} connectNulls />
                    ) : null}
                    <Line type="monotone" dataKey="drawdown" name={text.drawdown} stroke="#ff4466" strokeWidth={1.5} strokeDasharray="4 4" dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold text-foreground">{text.openTitle}</h3>
            {!openPositions || openPositions.items.length === 0 ? (
              <p className="text-xs text-secondary">{text.noOpen}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-[760px] w-full text-sm">
                  <thead className="text-xs text-secondary border-b border-white/10">
                    <tr>
                      <th className="text-left py-2 pr-2">{text.code}</th>
                      <th className="text-left py-2 pr-2">{text.openDate}</th>
                      <th className="text-right py-2 pr-2">{text.holdingDays}</th>
                      <th className="text-right py-2 pr-2">{text.quantity}</th>
                      <th className="text-right py-2 pr-2">{text.avgCost}</th>
                      <th className="text-right py-2 pr-2">{text.lastPrice}</th>
                      <th className="text-right py-2 pr-2">{text.marketValue}</th>
                      <th className="text-right py-2 pr-2">{text.pnl}</th>
                      <th className="text-right py-2 pr-2">{text.returnPct}</th>
                      <th className="text-right py-2">{text.weight}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {openPositions.items.map((row) => (
                      <tr key={`${row.market}-${row.stockCode}`} className="border-b border-white/5">
                        <td className="py-2 pr-2 font-mono text-foreground">{row.stockCode}</td>
                        <td className="py-2 pr-2 text-xs text-secondary">{row.openDate}</td>
                        <td className="py-2 pr-2 text-right">{row.holdingDays}</td>
                        <td className="py-2 pr-2 text-right">{row.quantity}</td>
                        <td className="py-2 pr-2 text-right">{row.avgCost == null ? text.none : Number(row.avgCost).toFixed(3)}</td>
                        <td className="py-2 pr-2 text-right">
                          {row.lastPrice == null ? text.none : Number(row.lastPrice).toFixed(2)}
                          {row.priceStale ? <Badge size="sm" variant="warning" className="ml-1">{text.priceStale}</Badge> : null}
                        </td>
                        <td className="py-2 pr-2 text-right">{formatMoney(row.marketValue)}</td>
                        <td className={`py-2 pr-2 text-right ${row.unrealizedPnl >= 0 ? 'text-[#ff4466]' : 'text-[#00ff88]'}`}>{formatMoney(row.unrealizedPnl)}</td>
                        <td className={`py-2 pr-2 text-right ${row.unrealizedPnl >= 0 ? 'text-[#ff4466]' : 'text-[#00ff88]'}`}>{formatSignedPct(row.returnPct)}</td>
                        <td className="py-2 text-right">{formatPct(row.weightPct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold text-foreground">{text.closedTitle}</h3>
            {!closed || closed.items.length === 0 ? (
              <p className="text-xs text-secondary">{text.noClosed}</p>
            ) : (
              <>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
                  <span>{text.best}: {closed.best ? `${closed.best.stockCode} ${formatSignedPct(closed.best.returnPct)}` : text.none}</span>
                  <span>{text.worst}: {closed.worst ? `${closed.worst.stockCode} ${formatSignedPct(closed.worst.returnPct)}` : text.none}</span>
                  <span>{text.avgWin}: {formatSignedPct(closed.avgWinPct)}</span>
                  <span>{text.avgLoss}: {formatSignedPct(closed.avgLossPct)}</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="min-w-[760px] w-full text-sm">
                    <thead className="text-xs text-secondary border-b border-white/10">
                      <tr>
                        <th className="text-left py-2 pr-2">{text.code}</th>
                        <th className="text-left py-2 pr-2">{text.openDate}</th>
                        <th className="text-left py-2 pr-2">{text.closeDate}</th>
                        <th className="text-right py-2 pr-2">{text.holdingDays}</th>
                        <th className="text-right py-2 pr-2">{text.quantity}</th>
                        <th className="text-right py-2 pr-2">{text.avgCost}</th>
                        <th className="text-right py-2 pr-2">{text.avgPrice}</th>
                        <th className="text-right py-2 pr-2">{text.pnl}</th>
                        <th className="text-right py-2">{text.returnPct}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {closed.items.map((row) => (
                        <tr key={`${row.market}-${row.stockCode}-${row.closeDate}`} className="border-b border-white/5">
                          <td className="py-2 pr-2 font-mono text-foreground">{row.stockCode}</td>
                          <td className="py-2 pr-2 text-xs text-secondary">{row.openDate}</td>
                          <td className="py-2 pr-2 text-xs text-secondary">{row.closeDate}</td>
                          <td className="py-2 pr-2 text-right">{row.holdingDays}</td>
                          <td className="py-2 pr-2 text-right">{row.quantity}</td>
                          <td className="py-2 pr-2 text-right">{row.avgCost == null ? text.none : Number(row.avgCost).toFixed(3)}</td>
                          <td className="py-2 pr-2 text-right">{row.avgPrice == null ? text.none : Number(row.avgPrice).toFixed(2)}</td>
                          <td className={`py-2 pr-2 text-right ${row.pnl >= 0 ? 'text-[#ff4466]' : 'text-[#00ff88]'}`}>{formatMoney(row.pnl)}</td>
                          <td className={`py-2 text-right ${row.pnl >= 0 ? 'text-[#ff4466]' : 'text-[#00ff88]'}`}>{formatSignedPct(row.returnPct)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </section>

          {report.notes.length > 0 ? (
            <section className="space-y-1">
              <h3 className="text-xs font-semibold text-foreground">{text.notesTitle}</h3>
              <ul className="list-disc space-y-1 pl-5 text-xs text-secondary">
                {report.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
};

type MetricTileProps = {
  label: string;
  value: string;
  hint?: string;
  tone?: 'up' | 'down';
};

const MetricTile: React.FC<MetricTileProps> = ({ label, value, hint, tone }) => (
  <div className="rounded-xl border border-white/10 bg-card/60 px-3 py-2">
    <p className="text-[11px] uppercase tracking-[0.16em] text-secondary">{label}</p>
    <p
      className={`mt-1 text-lg font-semibold ${
        tone === 'up' ? 'text-[#ff4466]' : tone === 'down' ? 'text-[#00ff88]' : 'text-foreground'
      }`}
    >
      {value}
    </p>
    {hint ? <p className="mt-0.5 text-[11px] text-secondary">{hint}</p> : null}
  </div>
);

export default PortfolioPerformancePanel;
