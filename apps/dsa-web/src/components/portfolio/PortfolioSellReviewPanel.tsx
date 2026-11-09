import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { History } from 'lucide-react';
import { portfolioApi } from '../../api/portfolio';
import { getParsedApiError } from '../../api/error';
import { Card, EmptyState, InlineAlert } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PORTFOLIO_SELL_REVIEW_TEXT } from '../../locales/featureText';
import { formatMoney, formatPct, formatSignedPct } from '../../utils/portfolioFormat';
import type { PortfolioSellReviewItem, PortfolioSellReviewResponse } from '../../types/portfolio';

type SellReviewPanelProps = {
  accountId?: number | null;
  asOf?: string | null;
};

const pnlClass = (value: number): string => (value >= 0 ? 'text-[#ff4466]' : 'text-[#00ff88]');

export const PortfolioSellReviewPanel: React.FC<SellReviewPanelProps> = ({
  accountId = null,
  asOf = null,
}) => {
  const { language } = useUiLanguage();
  const text = PORTFOLIO_SELL_REVIEW_TEXT[language];
  const [report, setReport] = useState<PortfolioSellReviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadReview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await portfolioApi.getSellReview({
        accountId: accountId ?? undefined,
        asOf: asOf ?? undefined,
      });
      setReport(data);
    } catch (err) {
      const parsed = getParsedApiError(err);
      setError(parsed.message);
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [accountId, asOf]);

  useEffect(() => {
    void loadReview();
  }, [loadReview]);

  const summary = report?.summary;
  const quality = report?.exitQuality;
  const items = report?.items ?? [];

  const renderAttribution = (rows: PortfolioSellReviewItem[], heading: string) =>
    rows.length > 0 ? (
      <div>
        <h3 className="mb-2 text-xs font-semibold text-foreground">{heading}</h3>
        <div className="space-y-2">
          {rows.map((row) => (
            <div
              key={`${row.stockCode}-${row.closeDate}`}
              className="rounded-xl border border-border/60 px-3 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-foreground">
                  {row.stockCode} {row.stockName || ''}
                </span>
                <span className={pnlClass(row.pnl)}>{formatSignedPct(row.returnPct)}</span>
                <span className="text-secondary">{row.exitReasonLabel}</span>
                <span className="text-secondary">
                  {text.holdingDays} {row.holdingDays} {text.daysUnit}
                </span>
              </div>
              <p className="mt-1 text-secondary">
                {text.entryThesis}: {row.entryReason || text.noThesis}
              </p>
            </div>
          ))}
        </div>
      </div>
    ) : null;

  return (
    <Card padding="md">
      <div className="mb-3 flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <History className="h-4 w-4 text-cyan" />
            {text.title}
          </h2>
          <p className="mt-1 text-xs text-secondary">{text.description}</p>
        </div>
        <button
          type="button"
          className="btn-secondary self-start text-sm"
          disabled={loading}
          onClick={() => void loadReview()}
        >
          {loading ? text.refreshing : text.refresh}
        </button>
      </div>

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

      {report && items.length === 0 ? (
        <EmptyState
          className="border-none bg-transparent py-6 shadow-none"
          title={text.empty}
          description={text.description}
          icon={<History className="h-6 w-6" />}
        />
      ) : null}

      {report && summary && items.length > 0 ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <div className="rounded-xl border border-border/60 px-3 py-2">
              <div className="text-xs text-secondary">{text.closedCount}</div>
              <div className="mt-1 text-sm font-semibold text-foreground">{summary.count}</div>
            </div>
            <div className="rounded-xl border border-border/60 px-3 py-2">
              <div className="text-xs text-secondary">{text.winRate}</div>
              <div className="mt-1 text-sm font-semibold text-foreground">
                {summary.winRatePct == null ? text.none : formatPct(summary.winRatePct)}
              </div>
            </div>
            <div className="rounded-xl border border-border/60 px-3 py-2">
              <div className="text-xs text-secondary">{text.realizedPnl}</div>
              <div className={`mt-1 text-sm font-semibold ${pnlClass(summary.realizedPnl)}`}>
                {formatMoney(summary.realizedPnl)}
              </div>
            </div>
            <div className="rounded-xl border border-border/60 px-3 py-2">
              <div className="text-xs text-secondary">{text.avgReturn}</div>
              <div className="mt-1 text-sm font-semibold text-foreground">
                {formatSignedPct(summary.avgReturnPct)}
              </div>
            </div>
            <div className="rounded-xl border border-border/60 px-3 py-2">
              <div className="text-xs text-secondary">{text.avgHoldingDays}</div>
              <div className="mt-1 text-sm font-semibold text-foreground">
                {summary.avgHoldingDays == null ? text.none : `${summary.avgHoldingDays} ${text.daysUnit}`}
              </div>
            </div>
          </div>

          {report.byReason.length > 0 ? (
            <div>
              <h3 className="mb-2 text-xs font-semibold text-foreground">{text.byReasonTitle}</h3>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-xs">
                  <thead>
                    <tr className="border-b border-border/60 text-secondary">
                      <th className="py-2 pr-2 text-left">{text.reason}</th>
                      <th className="py-2 pr-2 text-right">{text.count}</th>
                      <th className="py-2 pr-2 text-right">{text.winsLosses}</th>
                      <th className="py-2 pr-2 text-right">{text.winRate}</th>
                      <th className="py-2 pr-2 text-right">{text.totalPnl}</th>
                      <th className="py-2 pr-2 text-right">{text.avgReturn}</th>
                      <th className="py-2 text-right">{text.avgHolding}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.byReason.map((block) => (
                      <tr key={block.reason} className="border-b border-border/40">
                        <td className="py-2 pr-2 text-foreground">{block.label}</td>
                        <td className="py-2 pr-2 text-right">{block.count}</td>
                        <td className="py-2 pr-2 text-right">{block.wins}/{block.losses}</td>
                        <td className="py-2 pr-2 text-right">
                          {block.winRatePct == null ? text.none : formatPct(block.winRatePct)}
                        </td>
                        <td className={`py-2 pr-2 text-right ${pnlClass(block.totalPnl)}`}>
                          {formatMoney(block.totalPnl)}
                        </td>
                        <td className="py-2 pr-2 text-right">{formatSignedPct(block.avgReturnPct)}</td>
                        <td className="py-2 text-right">
                          {block.avgHoldingDays == null ? text.none : `${block.avgHoldingDays} ${text.daysUnit}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          {quality ? (
            <div className="rounded-xl border border-border/60 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-semibold text-foreground">{text.qualityTitle}</span>
                <span className="text-secondary">
                  {text.qualityEvaluated
                    .replace('{evaluated}', String(quality.evaluated))
                    .replace('{total}', String(items.length))}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                <span className="rounded-full border border-border/60 px-2 py-1 text-secondary">
                  {text.lockedGain}: {quality.lockedGain ?? 0}
                </span>
                <span className="rounded-full border border-border/60 px-2 py-1 text-secondary">
                  {text.soldEarly}: {quality.soldEarly ?? 0}
                </span>
                <span className="rounded-full border border-border/60 px-2 py-1 text-secondary">
                  {text.avoidedLoss}: {quality.avoidedLoss ?? 0}
                </span>
                <span className="rounded-full border border-border/60 px-2 py-1 text-secondary">
                  {text.soldLow}: {quality.soldLow ?? 0}
                </span>
              </div>
              {quality.evaluated < items.length ? (
                <p className="mt-2 text-xs text-secondary">{text.qualityPending}</p>
              ) : null}
            </div>
          ) : null}

          {renderAttribution(report.topWinners, text.winnersTitle)}
          {renderAttribution(report.topLosers, text.losersTitle)}

          <div>
            <h3 className="mb-2 text-xs font-semibold text-foreground">{text.itemsTitle}</h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-xs">
                <thead>
                  <tr className="border-b border-border/60 text-secondary">
                    <th className="py-2 pr-2 text-left">{text.code}</th>
                    <th className="py-2 pr-2 text-left">{text.closeDate}</th>
                    <th className="py-2 pr-2 text-left">{text.reason}</th>
                    <th className="py-2 pr-2 text-right">{text.holdingDays}</th>
                    <th className="py-2 pr-2 text-right">{text.returnPct}</th>
                    <th className="py-2 pr-2 text-right">{text.pnl}</th>
                    <th className="py-2 pr-2 text-right">{text.postSell5d}</th>
                    <th className="py-2 pr-2 text-right">{text.postSell10d}</th>
                    <th className="py-2 pr-2 text-right">{text.postSell20d}</th>
                    <th className="py-2 pr-2 text-left">{text.verdict}</th>
                    <th className="py-2 text-right">{text.entryScore}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <tr key={`${row.stockCode}-${row.closeDate}`} className="border-b border-border/40">
                      <td className="py-2 pr-2 text-foreground">
                        {row.stockCode}
                        {row.stockName ? <span className="ml-1 text-secondary">{row.stockName}</span> : null}
                      </td>
                      <td className="py-2 pr-2">{row.closeDate}</td>
                      <td className="py-2 pr-2">{row.exitReasonLabel}</td>
                      <td className="py-2 pr-2 text-right">{row.holdingDays}</td>
                      <td className={`py-2 pr-2 text-right ${pnlClass(row.pnl)}`}>
                        {formatSignedPct(row.returnPct)}
                      </td>
                      <td className={`py-2 pr-2 text-right ${pnlClass(row.pnl)}`}>
                        {formatMoney(row.pnl)}
                      </td>
                      <td className="py-2 pr-2 text-right">{formatSignedPct(row.postSell5dPct)}</td>
                      <td className="py-2 pr-2 text-right">{formatSignedPct(row.postSell10dPct)}</td>
                      <td className="py-2 pr-2 text-right">{formatSignedPct(row.postSell20dPct)}</td>
                      <td className="py-2 pr-2">
                        {row.exitVerdictLabel
                          ? `${row.exitVerdictLabel}${row.verdictHorizon ? ` (${row.verdictHorizon}${text.daysUnit})` : ''}`
                          : text.none}
                      </td>
                      <td className="py-2 text-right">{row.entryScore ?? text.none}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {report.notes.length > 0 ? (
            <div>
              <h3 className="mb-1 text-xs font-semibold text-foreground">{text.notesTitle}</h3>
              <ul className="list-disc space-y-1 pl-4 text-xs text-secondary">
                {report.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
};