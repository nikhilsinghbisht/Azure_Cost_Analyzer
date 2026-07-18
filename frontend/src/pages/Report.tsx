import { useEffect, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import Navbar from '../components/Navbar';
import { downloadExcel, getHistory } from '../lib/api';
import type { AnalysisIssue, AnalysisResult, AnalyzeResponse, IssueCategory } from '../lib/api';

const SEVERITY_STYLES = {
  high: {
    badge: 'bg-red-500/15 text-red-400 border border-red-500/30',
    card: 'border-red-500/25',
    dot: 'bg-red-400',
    label: 'text-red-400',
  },
  medium: {
    badge: 'bg-yellow-500/15 text-yellow-400 border border-yellow-500/30',
    card: 'border-yellow-500/25',
    dot: 'bg-yellow-400',
    label: 'text-yellow-400',
  },
  low: {
    badge: 'bg-green-500/15 text-green-400 border border-green-500/30',
    card: 'border-green-500/25',
    dot: 'bg-green-400',
    label: 'text-green-400',
  },
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  function handleCopy() {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }
  return (
    <button
      onClick={handleCopy}
      className="text-xs text-gray-400 hover:text-white transition-colors flex items-center gap-1"
    >
      {copied ? (
        <>
          <svg className="w-3.5 h-3.5 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
          <span className="text-green-400">Copied</span>
        </>
      ) : (
        <>
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
          Copy
        </>
      )}
    </button>
  );
}

// Fallback regex if AI didn't return a category
const ISSUE_TYPE_KEYWORDS: [RegExp, IssueCategory][] = [
  [/over.?provision|oversize|too large|large sku|oversized|general.?purpose.*dev|higher tier|more than needed|larger.*necess/i, 'Over-provisioned'],
  [/unused|idle|orphan|stopped|deallocat|empty|unattach|abandon|isolated.*no peer|no connect|zero app|no sites|not in use/i, 'Unused / Idle'],
  [/wrong tier|pricing tier|dev.?test|burstable.*general|should.*standard|should.*basic/i, 'Wrong Pricing Tier'],
  [/ha.*enabl|high.?avail.*non.?prod|zone.?redundant.*non|geo.?redundant.*non|redundan/i, 'Redundancy Config'],
  [/open.*inbound|wildcard|nsg.*open|public.*access|admin.*enabl|http only|exposure|misconfigur/i, 'Misconfigured'],
  [/security|open port|ssh|rdp.*internet|credential/i, 'Security Risk'],
  [/reserved|right.?siz|auto.?shutdown|spot|saving plan|scale.*down|commitment|autoscale/i, 'Optimization Opportunity'],
];

function inferIssueType(issue: AnalysisIssue): string {
  if (issue.category) return issue.category;
  const text = issue.issue;
  for (const [re, label] of ISSUE_TYPE_KEYWORDS) {
    if (re.test(text)) return label;
  }
  return 'Cost Saving'; // sensible default
}

const CATEGORY_STYLES: Record<string, { bg: string; text: string; border: string }> = {
  // Canonical categories (engine v2)
  'Overprovisioned':         { bg: 'bg-orange-500/15', text: 'text-orange-400', border: 'border-orange-500/30' },
  'Underprovisioned':        { bg: 'bg-amber-500/15',  text: 'text-amber-400',  border: 'border-amber-500/30' },
  'Idle':                    { bg: 'bg-gray-500/15',   text: 'text-gray-400',   border: 'border-gray-500/30' },
  'Cost Saving':             { bg: 'bg-emerald-500/15',text: 'text-emerald-400',border: 'border-emerald-500/30' },
  'Performance':             { bg: 'bg-yellow-500/15', text: 'text-yellow-400', border: 'border-yellow-500/30' },
  'Security':                { bg: 'bg-red-500/15',    text: 'text-red-400',    border: 'border-red-500/30' },
  'Governance':              { bg: 'bg-slate-500/15',  text: 'text-slate-300',  border: 'border-slate-500/30' },
  // Legacy categories (previously-stored analyses)
  'Over-provisioned':        { bg: 'bg-orange-500/15', text: 'text-orange-400', border: 'border-orange-500/30' },
  'Unused / Idle':           { bg: 'bg-gray-500/15',   text: 'text-gray-400',   border: 'border-gray-500/30' },
  'Wrong Pricing Tier':      { bg: 'bg-purple-500/15', text: 'text-purple-400', border: 'border-purple-500/30' },
  'Redundancy Config':       { bg: 'bg-blue-500/15',   text: 'text-blue-400',   border: 'border-blue-500/30' },
  'Misconfigured':           { bg: 'bg-yellow-500/15', text: 'text-yellow-400', border: 'border-yellow-500/30' },
  'Security Risk':           { bg: 'bg-red-500/15',    text: 'text-red-400',    border: 'border-red-500/30' },
  'Optimization Opportunity':{ bg: 'bg-emerald-500/15',text: 'text-emerald-400',border: 'border-emerald-500/30' },
};

const _DEFAULT_CAT_STYLE = { bg: 'bg-gray-500/15', text: 'text-gray-400', border: 'border-gray-500/30' };

function IssueCard({ issue }: { issue: AnalysisIssue }) {
  const s = SEVERITY_STYLES[issue.severity] ?? SEVERITY_STYLES['low'];
  const issueType = inferIssueType(issue);
  const catStyle = CATEGORY_STYLES[issueType] ?? _DEFAULT_CAT_STYLE;

  return (
    <div className={`bg-gray-800/50 border rounded-xl p-5 space-y-3 ${s.card}`}>
      {/* Title row */}
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className={`w-2 h-2 rounded-full flex-shrink-0 ${s.dot}`} />
          <div className="min-w-0">
            <span className="text-white font-medium text-sm block truncate">{issue.resource_name}</span>
            <span className="text-gray-500 text-xs">{issue.resource_type}</span>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap flex-shrink-0">
          <span className={`text-xs font-medium rounded-full px-2.5 py-0.5 border ${catStyle.bg} ${catStyle.text} ${catStyle.border}`}>
            {issueType}
          </span>
          <span className={`text-xs font-semibold rounded-full px-2.5 py-0.5 capitalize ${s.badge}`}>
            {issue.severity}
          </span>
        </div>
      </div>

      {/* Issue description */}
      <p className="text-gray-300 text-sm leading-relaxed">{issue.issue}</p>

      {/* Cost breakdown + savings — always render if there's anything to show */}
      <div className="space-y-2 pt-1 border-t border-gray-700/60">

        {/* Current → After fix → Saving pill row */}
        <div className="flex items-center gap-3 flex-wrap text-xs">
          {issue.current_monthly_cost_usd != null ? (
            <span className="text-gray-400">
              Current: <span className="text-white font-semibold">${issue.current_monthly_cost_usd.toFixed(2)}/mo</span>
            </span>
          ) : null}

          {issue.optimized_monthly_cost_usd != null && (
            <>
              {issue.current_monthly_cost_usd != null && <span className="text-gray-600">→</span>}
              <span className="text-gray-400">
                After fix: <span className="text-blue-400 font-semibold">${issue.optimized_monthly_cost_usd.toFixed(2)}/mo</span>
              </span>
            </>
          )}

          {issue.estimated_monthly_savings_usd != null && issue.estimated_monthly_savings_usd > 0 ? (
            <>
              <span className="text-gray-600">→</span>
              <span className="bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 rounded-full px-2.5 py-0.5 font-semibold">
                Save ~${issue.estimated_monthly_savings_usd.toFixed(0)}/mo
              </span>
            </>
          ) : issue.estimated_monthly_savings_usd === 0 ? (
            <span className="text-gray-500 text-xs italic">
              {issue.savings_reasoning?.includes('security') || issue.savings_reasoning?.includes('hygiene')
                ? 'No direct cost saving — security / hygiene concern'
                : 'No billing data — savings not quantifiable'}
            </span>
          ) : (
            <span className="text-gray-500 text-xs italic">Calculating savings…</span>
          )}
        </div>

        {/* Savings reasoning — always show if present and not just an internal cap note */}
        {issue.savings_reasoning && !issue.savings_reasoning.includes('[savings capped') && (
          <p className="text-xs text-gray-500 italic leading-relaxed">{issue.savings_reasoning}</p>
        )}

        {/* Fix commands */}
        {issue.fix_commands.length > 0 && (
          <>
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide pt-1">Fix commands</p>
            {issue.fix_commands.map((cmd, i) => (
              <div key={i} className="bg-gray-900 border border-gray-700 rounded-lg p-3 flex items-center justify-between gap-3">
                <code className="text-xs text-green-300 font-mono break-all">{cmd}</code>
                <CopyButton text={cmd} />
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

interface CostEntry {
  resource_id: string;
  resource_type: string;
  cost_usd: number;
  cost_original?: number;
  currency_original?: string;
}

export default function Report() {
  const { analysisId } = useParams<{ analysisId: string }>();
  const location = useLocation();
  const navigate = useNavigate();

  const stateData = (location.state as { data?: AnalyzeResponse } | null)?.data;

  const [analysis, setAnalysis] = useState<AnalysisResult | null>(stateData?.analysis ?? null);
  const [resourceGroup, setResourceGroup] = useState<string | null>(stateData?.resource_group ?? null);
  const [resourceCount, setResourceCount] = useState<number | null>(stateData?.resource_count ?? null);
  const [costBreakdown, setCostBreakdown] = useState<CostEntry[]>(
    (stateData?.analysis as (AnalysisResult & { actual_cost_breakdown?: CostEntry[] }) | null)?.actual_cost_breakdown ?? []
  );
  const [fetching, setFetching] = useState(!stateData?.analysis);
  const [exporting, setExporting] = useState(false);

  async function handleDownloadExcel() {
    if (!analysisId) return;
    setExporting(true);
    try {
      const safe = (resourceGroup ?? 'report').replace(/[^a-z0-9_-]/gi, '_');
      await downloadExcel(analysisId, `azure-cost-report_${safe}_${analysisId.slice(0, 8)}.xlsx`);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setExporting(false);
    }
  }

  // If state wasn't passed (e.g. opened from History or page refresh),
  // fall back to fetching from the /api/history endpoint.
  useEffect(() => {
    if (stateData?.analysis) return;
    setFetching(true);
    getHistory()
      .then((d) => {
        const item = d.analyses.find((a) => a.id === analysisId);
        if (item?.analysis_result) {
          const ar = item.analysis_result as AnalysisResult & { actual_cost_breakdown?: CostEntry[] };
          setAnalysis(ar);
          setResourceGroup(item.resource_group);
          setResourceCount(item.resources_scanned);
          setCostBreakdown(ar.actual_cost_breakdown ?? []);
        }
      })
      .catch(() => {/* leave analysis null → shows not-found UI */})
      .finally(() => setFetching(false));
  }, [analysisId]);

  if (fetching) {
    return (
      <div className="min-h-screen bg-gray-950">
        <Navbar />
        <div className="max-w-4xl mx-auto px-4 py-20 flex items-center justify-center gap-3 text-gray-400">
          <svg className="w-5 h-5 animate-spin text-azure" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
          </svg>
          Loading report…
        </div>
      </div>
    );
  }

  if (!analysis) {
    return (
      <div className="min-h-screen bg-gray-950">
        <Navbar />
        <div className="max-w-4xl mx-auto px-4 py-20 text-center space-y-4">
          <p className="text-gray-400">No report data found for ID: {analysisId}</p>
          <button
            onClick={() => navigate('/history')}
            className="text-azure hover:text-azure-light text-sm font-medium transition-colors"
          >
            View analysis history →
          </button>
        </div>
      </div>
    );
  }

  const highCount = analysis.issues.filter((i) => i.severity === 'high').length;
  const medCount = analysis.issues.filter((i) => i.severity === 'medium').length;
  const lowCount = analysis.issues.filter((i) => i.severity === 'low').length;
  const savings = analysis.total_estimated_monthly_savings_usd;

  return (
    <div className="min-h-screen bg-gray-950">
      <Navbar />

      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
        {/* Header */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0">
            <button
              onClick={() => navigate('/')}
              className="text-gray-400 hover:text-white transition-colors mt-1 flex-shrink-0"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
              </svg>
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h1 className="text-2xl font-bold text-white">Analysis Report</h1>
                {resourceGroup && (
                  <span className="text-xs font-medium text-azure bg-azure/10 border border-azure/25 rounded-full px-2.5 py-0.5">
                    {resourceGroup}
                  </span>
                )}
              </div>
              <p className="text-gray-600 text-xs mt-0.5 font-mono truncate">{analysisId}</p>
            </div>
          </div>

          {/* Download Excel button */}
          {analysis && (
            <button
              onClick={() => void handleDownloadExcel()}
              disabled={exporting}
              className="flex items-center gap-2 text-sm font-medium text-emerald-400 hover:text-white bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 hover:border-emerald-400/50 rounded-lg px-4 py-2 transition-all disabled:opacity-50 flex-shrink-0"
            >
              {exporting ? (
                <>
                  <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
                  </svg>
                  Exporting…
                </>
              ) : (
                <>
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                  </svg>
                  Download Excel
                </>
              )}
            </button>
          )}
        </div>

        {/* Summary card */}
        <div className="bg-gray-800/50 border border-gray-700 rounded-2xl p-6 space-y-5">
          <h2 className="text-base font-semibold text-white">Executive Summary</h2>
          <p className="text-gray-300 text-sm leading-relaxed">{analysis.summary}</p>

          {/* Key metrics — resources scanned, issues by severity, savings */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 pt-1">
            <div className="bg-gray-900/60 rounded-xl p-4 text-center col-span-2 sm:col-span-1">
              <p className="text-2xl font-bold text-white">{resourceCount ?? '—'}</p>
              <p className="text-xs text-gray-500 mt-0.5">Resources</p>
            </div>
            <div className="bg-gray-900/60 rounded-xl p-4 text-center">
              <p className="text-2xl font-bold text-red-400">{highCount}</p>
              <p className="text-xs text-gray-500 mt-0.5">High</p>
            </div>
            <div className="bg-gray-900/60 rounded-xl p-4 text-center">
              <p className="text-2xl font-bold text-yellow-400">{medCount}</p>
              <p className="text-xs text-gray-500 mt-0.5">Medium</p>
            </div>
            <div className="bg-gray-900/60 rounded-xl p-4 text-center">
              <p className="text-2xl font-bold text-green-400">{lowCount}</p>
              <p className="text-xs text-gray-500 mt-0.5">Low</p>
            </div>
            <div className="bg-gray-900/60 rounded-xl p-4 text-center">
              <p className="text-2xl font-bold text-emerald-400">
                {savings != null ? `$${savings.toFixed(0)}` : '$0'}
              </p>
              <p className="text-xs text-gray-500 mt-0.5">Est. /mo savings</p>
            </div>
          </div>
        </div>

        {/* Issues */}
        {analysis.issues.length > 0 && (
          <section className="space-y-4">
            <h2 className="text-base font-semibold text-white">
              Issues Found
              <span className="ml-2 text-gray-500 font-normal text-sm">({analysis.issues.length})</span>
            </h2>
            {(['high', 'medium', 'low'] as const).map((sev) => {
              const filtered = analysis.issues.filter((i) => i.severity === sev);
              if (filtered.length === 0) return null;
              const s = SEVERITY_STYLES[sev];
              return (
                <div key={sev} className="space-y-3">
                  <p className={`text-xs font-semibold uppercase tracking-wider ${s.label}`}>
                    {sev} severity
                  </p>
                  {filtered.map((issue, i) => (
                    <IssueCard key={i} issue={issue} />
                  ))}
                </div>
              );
            })}
          </section>
        )}

        {/* Actual Cost Breakdown */}
        {costBreakdown.length > 0 && (
          <section className="space-y-4">
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold text-white">Actual Cost Breakdown</h2>
              <span className="text-xs text-gray-500 bg-gray-800 border border-gray-700 rounded-full px-2.5 py-0.5">
                Month-to-date · Live from Azure
              </span>
            </div>
            <div className="bg-gray-800/50 border border-gray-700 rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-700">
                    <th className="text-left text-xs font-semibold text-gray-500 uppercase tracking-wide px-5 py-3">Resource</th>
                    <th className="text-left text-xs font-semibold text-gray-500 uppercase tracking-wide px-5 py-3 hidden sm:table-cell">Type</th>
                    <th className="text-right text-xs font-semibold text-gray-500 uppercase tracking-wide px-5 py-3">MTD Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-700/50">
                  {[...costBreakdown]
                    .sort((a, b) => b.cost_usd - a.cost_usd)
                    .map((entry, i) => {
                      const shortId = entry.resource_id.split('/').pop() ?? entry.resource_id;
                      const shortType = entry.resource_type.split('/').pop() ?? entry.resource_type;
                      return (
                        <tr key={i} className="hover:bg-gray-700/20 transition-colors">
                          <td className="px-5 py-3 text-gray-200 font-medium truncate max-w-xs" title={entry.resource_id}>
                            {shortId}
                          </td>
                          <td className="px-5 py-3 text-gray-500 text-xs hidden sm:table-cell">{shortType}</td>
                          <td className="px-5 py-3 text-right font-mono">
                            <div className="flex flex-col items-end gap-0.5">
                              <span className={entry.cost_usd > 10 ? 'text-red-400' : entry.cost_usd > 1 ? 'text-yellow-400' : 'text-gray-400'}>
                                ${entry.cost_usd.toFixed(2)}
                              </span>
                              {entry.currency_original && entry.currency_original !== 'USD' && entry.cost_original != null && (
                                <span className="text-gray-600 text-xs">
                                  {entry.currency_original === 'INR' ? '₹' : entry.currency_original + ' '}
                                  {entry.cost_original.toFixed(2)}
                                </span>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
                <tfoot>
                  <tr className="border-t border-gray-700 bg-gray-900/40">
                    <td className="px-5 py-3 text-xs font-semibold text-gray-400" colSpan={2}>
                      Total this month
                      {costBreakdown[0]?.currency_original && costBreakdown[0].currency_original !== 'USD' && (
                        <span className="ml-1.5 text-gray-600 font-normal">(converted to USD)</span>
                      )}
                    </td>
                    <td className="px-5 py-3 text-right font-mono font-bold text-white">
                      ${costBreakdown.reduce((s, c) => s + c.cost_usd, 0).toFixed(2)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </section>
        )}

        {/* Recommendations */}
        {analysis.general_recommendations.length > 0 && (
          <section className="space-y-4">
            <h2 className="text-base font-semibold text-white">General Recommendations</h2>
            <div className="bg-gray-800/50 border border-gray-700 rounded-xl divide-y divide-gray-700">
              {analysis.general_recommendations.map((rec, i) => (
                <div key={i} className="flex items-start gap-3 px-5 py-4">
                  <span className="text-azure mt-0.5 flex-shrink-0">
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                    </svg>
                  </span>
                  <p className="text-gray-300 text-sm leading-relaxed">{rec}</p>
                </div>
              ))}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
