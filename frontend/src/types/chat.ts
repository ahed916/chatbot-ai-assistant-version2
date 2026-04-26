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
  dbId?: string;        // database UUID, may differ from local session ID
  title: string;
  messages: ChatMessage[];
  createdAt: Date;
  updatedAt: Date;
  isProactive?: boolean;
}

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
  status?: "critical" | "warning" | "good" | "ok" | "info";
  context?: string;
}

export interface ChartConfig {
  type: "bar" | "pie" | "line" | "donut" | "area" | "stackedBar";
  title: string;
  data: Record<string, unknown>[];
  xKey?: string;
  yKey?: string;
  nameKey?: string;
  valueKey?: string;
  colors?: string[];
  insight?: string;
}

export interface DashboardPayload {
  type: "dashboard" | "quick_stat" | "no_data" | "clarification";
  title?: string;
  summary?: string;
  generated_at?: string;
  kpis?: KPI[];
  charts?: ChartConfig[];
  label?: string;
  value?: string | number;
  context?: string;
  message?: string;
  chartType?: string;
  data?: Record<string, unknown>[];
}