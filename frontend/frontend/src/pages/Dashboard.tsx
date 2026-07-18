import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Navbar from '../components/Navbar';
import ProgressTracker from '../components/ProgressTracker';
import { getResourceGroups, openProgressSocket, runAnalysis, type AnalyzeResponse, type ResourceGroup } from '../lib/api';

export default function Dashboard() {
  const navigate = useNavigate();

  const [groups, setGroups] = useState<ResourceGroup[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(true);
  const [groupsError, setGroupsError] = useState('');

  const [selectedRG, setSelectedRG] = useState('');
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [progressMessages, setProgressMessages] = useState<string[]>([]);
  const [analyzeError, setAnalyzeError] = useState('');

  useEffect(() => {
    getResourceGroups()
      .then((data) => {
        setGroups(data.resource_groups);
        if (data.resource_groups.length > 0) {
          setSelectedRG(data.resource_groups[0].name);
        }
      })
      .catch((err) => setGroupsError(err instanceof Error ? err.message : 'Failed to load resource groups'))
      .finally(() => setGroupsLoading(false));
  }, []);

  async function handleRunAnalysis() {
    if (!selectedRG || isAnalyzing) return;

    setIsAnalyzing(true);
    setProgressMessages([]);
    setAnalyzeError('');

    const analysisId = crypto.randomUUID();
    const messages: string[] = [];

    // Open the WebSocket and WAIT for it to be fully open before sending the POST.
    // This prevents the race condition where the server starts pushing progress
    // messages before the client socket is listening.
    const { ws, ready } = openProgressSocket(analysisId, (msg) => {
      messages.push(msg);
      setProgressMessages([...messages]);
    });

    try {
      await ready;
    } catch {
      setAnalyzeError('Could not connect to progress stream. Analysis will still run — check History for results.');
    }

    let result: AnalyzeResponse | null = null;
    try {
      result = await runAnalysis(selectedRG, analysisId);
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : 'Analysis failed');
      setIsAnalyzing(false);
      ws.close();
      return;
    }

    // Brief pause so the final "Analysis complete" WebSocket message can arrive
    // and render before we navigate away.
    await new Promise((r) => setTimeout(r, 600));

    ws.close();
    setIsAnalyzing(false);

    navigate(`/report/${result.analysis_id}`, { state: { data: result } });
  }

  return (
    <div className="min-h-screen bg-gray-950">
      <Navbar />

      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
        {/* Page header */}
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-gray-400 text-sm mt-1">
            Select an Azure resource group and run an AI-powered cost analysis.
          </p>
        </div>

        {/* Analysis card */}
        <div className="bg-gray-800/50 border border-gray-700 rounded-2xl p-6 space-y-6">
          <h2 className="text-base font-semibold text-white">Run Analysis</h2>

          {groupsError && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 text-sm text-red-400">
              {groupsError}
            </div>
          )}

          <div className="flex flex-col sm:flex-row gap-3">
            {/* Resource group selector */}
            <div className="flex-1">
              <label className="block text-sm font-medium text-gray-400 mb-1.5">
                Resource Group
              </label>
              {groupsLoading ? (
                <div className="h-10 bg-gray-700 rounded-lg animate-pulse" />
              ) : (
                <select
                  value={selectedRG}
                  onChange={(e) => setSelectedRG(e.target.value)}
                  disabled={isAnalyzing}
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-4 py-2.5 text-white text-sm focus:outline-none focus:border-azure focus:ring-1 focus:ring-azure disabled:opacity-50 transition-colors appearance-none cursor-pointer"
                >
                  {groups.length === 0 && (
                    <option value="">No resource groups found</option>
                  )}
                  {groups.map((g) => (
                    <option key={g.name} value={g.name}>
                      {g.name} — {g.location}
                    </option>
                  ))}
                </select>
              )}
            </div>

            {/* Run button */}
            <div className="sm:self-end">
              <button
                onClick={handleRunAnalysis}
                disabled={!selectedRG || isAnalyzing || groupsLoading}
                className="w-full sm:w-auto flex items-center justify-center gap-2 bg-azure hover:bg-azure-dark disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold rounded-lg px-6 py-2.5 text-sm transition-colors"
              >
                {isAnalyzing ? (
                  <>
                    <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                    </svg>
                    Analyzing…
                  </>
                ) : (
                  <>
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                    </svg>
                    Run Analysis
                  </>
                )}
              </button>
            </div>
          </div>

          {/* Info row */}
          {selectedRG && !isAnalyzing && progressMessages.length === 0 && (
            <div className="flex items-start gap-2 text-xs text-gray-500 bg-gray-700/30 rounded-lg px-4 py-3">
              <svg className="w-4 h-4 flex-shrink-0 mt-0.5 text-azure" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <span>
                The analysis will scan all resources in <strong className="text-gray-300">{selectedRG}</strong> and
                use OpenAI to identify cost optimizations. This may take 20–60 seconds.
              </span>
            </div>
          )}

          {/* Progress tracker */}
          <ProgressTracker
            messages={progressMessages}
            isRunning={isAnalyzing}
            error={analyzeError}
          />
        </div>

        {/* Quick stats */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {[
            { label: 'Resource Groups', value: groups.length, icon: '🗂️' },
            { label: 'AI Model', value: 'GPT-4o', icon: '🤖' },
            { label: 'Data Source', value: 'Azure CLI', icon: '☁️' },
          ].map((stat) => (
            <div key={stat.label} className="bg-gray-800/40 border border-gray-700 rounded-xl px-5 py-4 flex items-center gap-4">
              <span className="text-2xl">{stat.icon}</span>
              <div>
                <p className="text-xs text-gray-500">{stat.label}</p>
                <p className="text-base font-semibold text-white">{stat.value}</p>
              </div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}
