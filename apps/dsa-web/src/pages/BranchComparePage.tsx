import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { GitBranch, RefreshCw } from 'lucide-react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { paperTradingApi } from '../api/paperTrading';
import { EmptyState, InlineAlert } from '../components/common';
import type {
  BranchComparisonItem,
  BranchComparisonResponse,
  BranchGateItem,
} from '../types/paperBranchCompare';

// 用户纪律：比接口转正门槛更严，顶部常驻醒目提示
const DISCIPLINE_NOTE = '纪律提示：30+ 笔平仓前不下结论 —— 以下门槛状态仅供参考，样本不足时任何"达标"都不构成转正依据。';

// 中性色系（净值曲线非涨跌信号；状态不用绿=好的暗示色）
const SERIES_COLORS = ['#38bdf8', '#a78bfa', '#f59e0b', '#f472b6', '#34d399', '#fb7185'];
const REFERENCE_COLOR = '#94a3b8';

const cardClass = 'rounded-2xl border border-border/60 bg-card p-6 shadow-soft-card';

function branchKey(branch: BranchComparisonItem): string {
  return `b${branch.accountId}`;
}

function branchName(branch: BranchComparisonItem): string {
  const base = branch.label || branch.accountName || `账户 ${branch.accountId}`;
  return branch.isReference ? `${base}（live 对照）` : base;
}

function fmt(value: number | null | undefined, digits = 2, suffix = ''): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toFixed(digits)}${suffix}`;
}

// hover 卡片：按数据类别分组，组间分隔线（用户明确偏好）
const NavTooltip: React.FC<{
  active?: boolean;
  payload?: Array<{ dataKey?: string | number; name?: string; value?: number | string; color?: string }>;
  label?: string | number;
}> = ({ active, payload, label }) => {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-xl border border-border/60 bg-card px-4 py-3 text-xs shadow-soft-card">
      <div className="font-semibold text-foreground">{String(label ?? '')}</div>
      <div className="my-2 border-t border-border/60" />
      <div className="space-y-1.5">
        <div className="text-secondary-text">TWR 净值（共同窗口首日 = 100）</div>
        {payload.map((entry) => (
          <div key={String(entry.dataKey)} className="flex items-center justify-between gap-6">
            <span className="flex items-center gap-2 text-secondary-text">
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: entry.color }} />
              {entry.name}
            </span>
            <span className="font-mono text-foreground">{fmt(Number(entry.value))}</span>
          </div>
        ))}
      </div>
      <div className="my-2 border-t border-border/60" />
      <div className="text-secondary-text">净值越高越好；虚线为 live 对照账户</div>
    </div>
  );
};

const GATE_STATUS_TEXT: Record<BranchGateItem['status'], { text: string; className: string }> = {
  pass: { text: '达标', className: 'border-cyan/30 bg-cyan/10 text-cyan' },
  fail: { text: '未达', className: 'border-warning/30 bg-warning/10 text-warning' },
  na: { text: '不适用', className: 'border-border/60 bg-muted/30 text-secondary-text' },
};

const GateTable: React.FC<{ branch: BranchComparisonItem }> = ({ branch }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-left text-sm">
      <thead>
        <tr className="border-b border-border/60 text-xs text-secondary-text">
          <th className="py-2 pr-4 font-medium">门槛</th>
          <th className="py-2 pr-4 font-medium">当前值</th>
          <th className="py-2 pr-4 font-medium">阈值</th>
          <th className="py-2 font-medium">状态</th>
        </tr>
      </thead>
      <tbody>
        {branch.gates.map((gate) => {
          const status = GATE_STATUS_TEXT[gate.status] ?? GATE_STATUS_TEXT.na;
          return (
            <tr key={gate.name} className="border-b border-border/40 last:border-0" title={gate.hint}>
              <td className="py-2.5 pr-4 text-foreground">{gate.name}</td>
              <td className="py-2.5 pr-4 font-mono text-foreground">{fmt(gate.value)}</td>
              <td className="py-2.5 pr-4 text-secondary-text">{gate.threshold}</td>
              <td className="py-2.5">
                <span className={`inline-block rounded-full border px-2.5 py-0.5 text-xs ${status.className}`}>
                  {status.text}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
);

const SummaryCard: React.FC<{ branch: BranchComparisonItem; color: string }> = ({ branch, color }) => {
  const m = branch.metrics;
  const rows: Array<[string, string]> = [
    ['窗口收益', fmt(m.returnPct, 2, '%')],
    ['最大回撤', fmt(m.maxDrawdownPct, 2, '%')],
    ['相对 live 超额', fmt(m.excessPct, 2, '%')],
    ['成交笔数（买/卖各计 1）', String(m.trades)],
    ['滚动 20 日胜率', m.rollingWin === null ? '—' : fmt(m.rollingWin * 100, 1, '%')],
    ['连续榜首天数', String(m.topDays)],
    ['综合分', fmt(branch.score, 1)],
  ];
  return (
    <div className={cardClass}>
      <div className="mb-4 flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: color }} />
        <h3 className="text-sm font-semibold text-foreground">{branchName(branch)}</h3>
        {branch.forkDate ? (
          <span className="text-xs text-secondary-text">分叉 {branch.forkDate}</span>
        ) : null}
      </div>
      <dl className="space-y-2 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="flex items-center justify-between gap-4">
            <dt className="text-secondary-text">{label}</dt>
            <dd className="font-mono text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-4 border-t border-border/60 pt-3 text-xs text-secondary-text">
        期末权益 / 平仓笔数 / 被风控拦截笔数：对比接口未提供，暂无数据源（拦截明细见 /runs 审计，后续接入）。
      </p>
    </div>
  );
};

const BranchComparePage: React.FC = () => {
  const [data, setData] = useState<BranchComparisonResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 只读 GET，不触发任何写操作
      const response = await paperTradingApi.comparison();
      setData(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const branches = useMemo(() => data?.branches ?? [], [data]);
  const candidates = branches.filter((b) => !b.isReference);
  const hasSeries = branches.some((b) => b.series.length > 0);

  const colorOf = useMemo(() => {
    const map = new Map<string, string>();
    let idx = 0;
    branches.forEach((b) => {
      if (b.isReference) {
        map.set(branchKey(b), REFERENCE_COLOR);
      } else {
        map.set(branchKey(b), SERIES_COLORS[idx % SERIES_COLORS.length]);
        idx += 1;
      }
    });
    return map;
  }, [branches]);

  // 各分支净值按日期并集对齐，叠在同一张图
  const chartRows = useMemo(() => {
    const byDate = new Map<string, Record<string, number | string>>();
    branches.forEach((b) => {
      b.series.forEach((point) => {
        const row = byDate.get(point.date) ?? { date: point.date };
        row[branchKey(b)] = point.nav;
        byDate.set(point.date, row);
      });
    });
    return Array.from(byDate.values()).sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }, [branches]);

  const forkLine = data?.window.start ?? null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-foreground">
            <GitBranch className="h-5 w-5 text-cyan" />
            分支对比
          </h1>
          <p className="mt-1 text-sm text-secondary-text">
            纸面风控分支 vs live 对照 · TWR 净值（首日归一 100）· 只读页面
            {data ? ` · 截止 ${data.asOf} · 共同窗口 ${data.window.start ?? '—'} ~ ${data.window.end ?? '—'}（${data.window.days} 个交易日）` : ''}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="flex items-center gap-2 rounded-xl border border-border/60 bg-card px-4 py-2 text-sm text-secondary-text transition-colors hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      <InlineAlert variant="warning" title="先说纪律" message={DISCIPLINE_NOTE} />

      {error ? (
        <InlineAlert variant="danger" title="对比数据加载失败" message={`${error}（本页只读，可稍后重试；不影响纸面交易运行）`} />
      ) : null}

      {data?.warnings?.length ? (
        <InlineAlert variant="info" title="接口提示" message={data.warnings.join('；')} />
      ) : null}

      {!loading && !error && (!hasSeries || candidates.length === 0) ? (
        <EmptyState
          icon={<GitBranch className="h-8 w-8" />}
          title="还没有可对比的风控分支"
          description="从纸面账户（账户 2）fork 分支账户后，这里会展示各分支与 live 对照的净值曲线和转正门槛。建议分支：A = 仅盈亏比过滤 RR≥1.0；B = RR≥1.0 + 全冷却 5 日。fork 可用官方克隆脚本 scripts/paper_trading_clone.py，分支净值依赖日常纸面运行持续回填快照。"
        />
      ) : null}

      {hasSeries ? (
        <div className={cardClass}>
          <h2 className="mb-2 text-sm font-semibold text-foreground">各分支 TWR 净值曲线</h2>
          <p className="mb-4 text-xs text-secondary-text">
            共同分叉日窗口，首日归一 100；竖线为分叉日；图例在底部，悬停查看分组明细。
          </p>
          <div className="h-[380px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartRows} margin={{ top: 24, right: 36, bottom: 12, left: 12 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--color-border) / 0.4)" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} tickMargin={10} minTickGap={28} />
                <YAxis tick={{ fontSize: 11 }} tickMargin={8} domain={['auto', 'auto']} width={56} />
                <Tooltip content={<NavTooltip />} />
                <Legend verticalAlign="bottom" align="center" iconType="plainline" wrapperStyle={{ paddingTop: 16, fontSize: 12 }} />
                {forkLine ? (
                  <ReferenceLine
                    x={forkLine}
                    stroke={REFERENCE_COLOR}
                    strokeDasharray="4 4"
                    label={{ value: '分叉日', position: 'insideTopRight', fontSize: 11, fill: REFERENCE_COLOR }}
                  />
                ) : null}
                {branches.map((b) => (
                  <Line
                    key={branchKey(b)}
                    type="monotone"
                    dataKey={branchKey(b)}
                    name={branchName(b)}
                    stroke={colorOf.get(branchKey(b))}
                    strokeWidth={b.isReference ? 1.5 : 2}
                    strokeDasharray={b.isReference ? '5 4' : undefined}
                    dot={false}
                    connectNulls
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      ) : null}

      {candidates.length > 0 ? (
        <div className="grid gap-6 lg:grid-cols-2">
          {candidates.map((b) => (
            <SummaryCard key={b.accountId} branch={b} color={colorOf.get(branchKey(b)) ?? REFERENCE_COLOR} />
          ))}
        </div>
      ) : null}

      {candidates.map((b) => (
        <div key={`gates-${b.accountId}`} className={cardClass}>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-foreground">转正门槛 · {branchName(b)}</h2>
            <span className="text-xs text-secondary-text">
              综合判定：{b.passed === null ? '—' : b.passed ? '全部达标（仍需满足 30+ 笔平仓纪律）' : '未全部达标'}
              （第 7 条参数稳健性在 DSA 分支上不适用，返回"不适用"且不影响判定）
            </span>
          </div>
          {b.gates.length > 0 ? (
            <GateTable branch={b} />
          ) : (
            <p className="text-sm text-secondary-text">接口未返回门槛明细。</p>
          )}
        </div>
      ))}
    </div>
  );
};

export default BranchComparePage;