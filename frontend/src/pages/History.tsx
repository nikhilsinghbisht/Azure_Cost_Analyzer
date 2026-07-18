import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Navbar from '../components/Navbar';
import { downloadExcel, getHistory, type HistoryItem } from '../lib/api';

const STATUS_STYLES: Record<string, string> = {
  completed: 'text-green-400 bg-green-400/10 border-green-400/25',
  running: 'text-azure bg-azure/10 border-azure/25',
  failed: 'text-red-400 bg-red-400/10 border-red-400/25',
  pending: 'text-gray-400 bg-gray-400/10 border-gray-400/25',
};

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function History() {
  const navigate = useNavigate();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [exportingId, setExportingId] = useState<string | null>(null);

  async function handleDownloadExcel(e: React.MouseEvent, item: HistoryItem) {
    e.stopPropagation();
    setExportingId(item.id);
    try {
      const safe = item.resource_group.replace(/[^a-z0-9_-]/gi, '_');
      await downloadExcel(item.id, `azure-cost-report_${safe}_${item.id.slice(0, 8)}.xlsx`);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setExportingId(null);
    }
  }

  function fetchHistory() {
    setLoading(true);
    setError('');
    getHistory()
      .then((d) => setItems(d.analyses))
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load history'))
      .finally(() => setLoading(false));
  }

  useEffect(() => { fetchHistory(); }, []);

  function openReport(item: HistoryItem) {
    if (!item.analysis_result) return;
    navigate(`/report/${item.id}`, {
      state: {
        data: {
          analysis_id: item.id,
          resource_group: item.resource_group,
          resource_count: item.resources_scanned,
          resources: [],
          analysis: item.analysis_result,
        },
      },
    });
  }

  return (
    <div className="min-h-screen bg-gray-950">
      <Navbar />

      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-white">Analysis History</h1>
            <p className="text-gray-400 text-sm mt-1">
              All past analyses for your account, newest first.
            </p>
          </div>
          <button
            onClick={fetchHistory}
            disabled={loading}
            className="flex items-center gap-2 text-sm font-medium text-gray-400 hover:text-white border border-gray-700 hover:border-gray-500 rounded-lg px-3 py-1.5 transition-colors disabled:opacity-50"
          >
            <svg className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Refresh
          </button>
        </div>

        {error && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 text-sm text-red-400">
            {error}
          </div>
        )}

        {loading ? (
          <div className="space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-20 bg-gray-800/50 border border-gray-700 rounded-xl animate-pulse" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-20 space-y-3">
            <div className="text-5xl">📊</div>
            <p className="text-gray-400 text-sm">No analyses yet.</p>
            <button
              onClick={() => navigate('/')}
              className="text-azure hover:text-azure-light text-sm font-medium transition-colors"
            >
              Run your first analysis →
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {items.map((item) => {
              const canOpen = item.status === 'completed' && item.analysis_result;
              return (
                <div
                  key={item.id}
                  onClick={() => canOpen && openReport(item)}
                  className={`bg-gray-800/50 border border-gray-700 rounded-xl px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-4 transition-colors ${
                    canOpen ? 'hover:border-gray-600 hover:bg-gray-800/70 cursor-pointer' : 'opacity-75'
                  }`}
                >
                  {/* Left: RG + date */}
                  <div className="flex-1 min-w-0">
                    <p className="text-white font-medium text-sm truncate">{item.resource_group}</p>
                    <p className="text-gray-500 text-xs mt-0.5">{formatDate(item.created_at)}</p>
                  </div>

                  {/* Stats */}
                  <div className="flex items-center gap-5 text-center shrink-0">
                    <div>
                      <p className="text-white font-semibold text-sm">{item.resources_scanned}</p>
                      <p className="text-gray-500 text-xs">Resources</p>
                    </div>
                    <div>
                      <p className="text-white font-semibold text-sm">{item.issues_found}</p>
                      <p className="text-gray-500 text-xs">Issues</p>
                    </div>
                    <div>
                      <p className="text-green-400 font-semibold text-sm">
                        {item.estimated_savings ?? '—'}
                      </p>
                      <p className="text-gray-500 text-xs">Savings</p>
                    </div>
                  </div>

                  {/* Status + download + arrow */}
                  <div className="flex items-center gap-3 shrink-0">
                    <span
                      className={`text-xs font-medium border rounded-full px-2.5 py-0.5 capitalize ${
                        STATUS_STYLES[item.status] ?? STATUS_STYLES.pending
                      }`}
                    >
                      {item.status}
                    </span>
                    {canOpen && (
                      <button
                        onClick={(e) => void handleDownloadExcel(e, item)}
                        disabled={exportingId === item.id}
                        title="Download Excel report"
                        className="flex items-center gap-1.5 text-xs font-medium text-emerald-400 hover:text-white bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 rounded-lg px-2.5 py-1 transition-all disabled:opacity-50"
                      >
                        {exportingId === item.id ? (
                          <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"/>
                          </svg>
                        ) : (
                          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                          </svg>
                        )}
                        Excel
                      </button>
                    )}
                    {canOpen && (
                      <svg className="w-4 h-4 text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
