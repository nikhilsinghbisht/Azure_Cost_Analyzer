import { clearAuth, getToken } from './auth';

// VITE_API_BASE is set at build time via env var (e.g. Render static site env)
const BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') ?? 'http://localhost:8000';

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers as Record<string, string>),
  };

  const res = await fetch(`${BASE}${path}`, { ...options, headers });

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    const detail = typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`;

    // Only treat 401 as "session expired" on protected APIs — not on login/signup
    // (those also return 401 for wrong password, which is not an expired session).
    const isAuthEndpoint = path.startsWith('/api/auth/');
    if (res.status === 401 && !isAuthEndpoint) {
      clearAuth();
      window.location.replace('/login');
      throw new ApiError(detail || 'Session expired. Please log in again.', 401);
    }

    throw new ApiError(detail, res.status);
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface AuthPayload {
  token: string;
  user: { id: string; email: string };
}

export const signup = (email: string, password: string) =>
  request<AuthPayload>('/api/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });

export const login = (email: string, password: string) =>
  request<AuthPayload>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });

// ---------------------------------------------------------------------------
// Resource groups
// ---------------------------------------------------------------------------

export interface ResourceGroup {
  name: string;
  location: string;
  tags: Record<string, string>;
  provisioning_state: string;
}

export const getResourceGroups = () =>
  request<{ resource_groups: ResourceGroup[]; count: number }>('/api/resource-groups');

// ---------------------------------------------------------------------------
// Analysis
// ---------------------------------------------------------------------------

export type IssueCategory =
  // Canonical categories (recommendation engine v2)
  | 'Idle'
  | 'Overprovisioned'
  | 'Underprovisioned'
  | 'Cost Saving'
  | 'Performance'
  | 'Security'
  // Legacy categories (kept so previously-stored analyses still render)
  | 'Over-provisioned'
  | 'Unused / Idle'
  | 'Wrong Pricing Tier'
  | 'Redundancy Config'
  | 'Misconfigured'
  | 'Security Risk'
  | 'Optimization Opportunity';

export interface AnalysisIssue {
  resource_name: string;
  resource_type: string;
  severity: 'high' | 'medium' | 'low';
  category?: IssueCategory | string;
  issue: string;
  current_monthly_cost_usd: number | null;
  optimized_monthly_cost_usd: number | null;
  estimated_monthly_savings_usd: number | null;
  savings_reasoning: string | null;
  fix_commands: string[];
  // Rich recommendation fields (engine v2 — all optional/back-compatible)
  title?: string;
  description?: string;
  reason?: string;
  current_configuration?: string | null;
  recommended_configuration?: string | null;
  confidence_score?: number;
  confidence_pct?: number;
  risk_level?: 'Low' | 'Medium' | 'High';
  priority?: string;
  documentation_url?: string | null;
  is_alternative?: boolean;
}

export interface ResourceRecommendationGroup {
  resource_name: string;
  resource_type?: string;
  recommendations: AnalysisIssue[];
  total_savings_usd: number;
  top_recommendation?: string | null;
}

export interface AnalysisResult {
  summary: string;
  total_estimated_monthly_savings_usd: number | null;
  issues: AnalysisIssue[];
  general_recommendations: string[];
  resource_recommendations?: ResourceRecommendationGroup[];
  recommendation_engine?: {
    version: number;
    deterministic_count: number;
    ai_supplemented_count: number;
    resources_analyzed: number;
  };
}

export interface AnalyzeResponse {
  analysis_id: string;
  resource_group: string;
  resource_count: number;
  resources: unknown[];
  analysis: AnalysisResult;
}

export const runAnalysis = (resource_group: string, analysis_id: string) =>
  request<AnalyzeResponse>('/api/analyze', {
    method: 'POST',
    body: JSON.stringify({ resource_group, analysis_id }),
  });

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

export interface HistoryItem {
  id: string;
  resource_group: string;
  resources_scanned: number;
  issues_found: number;
  estimated_savings: string | null;
  analysis_result: AnalysisResult | null;
  status: string;
  created_at: string;
}

export const getHistory = () =>
  request<{ analyses: HistoryItem[]; count: number }>('/api/history');

// ---------------------------------------------------------------------------
// Excel export
// ---------------------------------------------------------------------------

/**
 * Returns the absolute URL for downloading an analysis as .xlsx.
 * Use as an <a href> so the browser handles the download natively.
 */
export function getExcelExportUrl(analysisId: string): string {
  const token = getToken();
  // Appended as query param so a plain <a> tag can carry auth.
  return `${BASE}/api/analyses/${analysisId}/export/excel?token=${token ?? ''}`;
}

/**
 * Programmatic download of the Excel report — fetches with auth header,
 * then triggers a browser download via a temporary <a> element.
 */
export async function downloadExcel(analysisId: string, filename?: string): Promise<void> {
  const token = getToken();
  const res = await fetch(`${BASE}/api/analyses/${analysisId}/export/excel`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });

  if (res.status === 401) {
    clearAuth();
    window.location.replace('/login');
    throw new ApiError('Session expired.', 401);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    throw new ApiError(body.detail ?? `HTTP ${res.status}`, res.status);
  }

  const blob = await res.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename ?? `azure-cost-report-${analysisId.slice(0, 8)}.xlsx`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// WebSocket progress helper
// ---------------------------------------------------------------------------

/**
 * Opens a WebSocket to the progress endpoint and returns a Promise that
 * resolves once the socket is open (or rejects on error), plus the socket
 * itself so callers can close it when done.
 *
 * Waiting for `onopen` before sending the POST avoids the race condition
 * where the server starts pushing messages before the client is listening.
 */
export function openProgressSocket(
  analysisId: string,
  onMessage: (msg: string) => void,
  onClose?: () => void,
): { ws: WebSocket; ready: Promise<void> } {
  const wsBase = BASE.replace(/^http/, 'ws');
  const ws = new WebSocket(`${wsBase}/ws/progress/${analysisId}`);

  const ready = new Promise<void>((resolve, reject) => {
    ws.onopen = () => resolve();
    ws.onerror = () => reject(new Error('WebSocket failed to connect'));
  });

  ws.onmessage = (e) => {
    const data = JSON.parse(e.data as string) as { progress: string };
    onMessage(data.progress);
  };

  ws.onclose = () => onClose?.();

  return { ws, ready };
}
