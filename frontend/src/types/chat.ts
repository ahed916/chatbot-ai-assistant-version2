export interface ChartData {
  label: string;
  value: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  isProactive?: boolean;
  isDashboard?: boolean;
  dashboard?: DashboardPayload;
  latencyMs?: number;
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: Date;
  updatedAt: Date;
  isProactive?: boolean;
}

// ── Added: required by useChatState for risk alert panel ─────────────────────
export interface RiskAlert {
  id: string;
  message: string;
  critical_count: number;
  overall_health: string;
  recommendations: string[];
  risks: unknown[];
  checked_at: string | null;
  read: boolean;
  conversationId?: string;
}

export interface KPI {
  label: string;
  value: number | string;
  trend?: "up" | "down" | "stable";
  // Added "good" — the agent returns this value, was missing from union
  status?: "critical" | "warning" | "good" | "ok" | "info";
  context?: string;
}

export interface ChartConfig {
  // Added donut / area / stackedBar — agent may return these types
  type: "bar" | "pie" | "line" | "donut" | "area" | "stackedBar";
  title: string;
  data: Record<string, unknown>[];
  xKey?: string;
  yKey?: string;
  nameKey?: string;
  valueKey?: string;
  colors?: string[];   // Added — agent sends hex colors per chart
  insight?: string;
}

export interface DashboardPayload {
  // Added "no_data" — returned when Redmine has 0 issues
  type: "dashboard" | "quick_stat" | "no_data";
  title?: string;
  summary?: string;
  generated_at?: string;
  kpis?: KPI[];
  charts?: ChartConfig[];
  // Added: quick_stat fields — returned for single-number answers
  label?: string;
  value?: string | number;
  context?: string;
  // Added: no_data explanation text
  message?: string;
  // legacy simple shape (keep for backwards compat)
  chartType?: string;
  data?: Record<string, unknown>[];
}