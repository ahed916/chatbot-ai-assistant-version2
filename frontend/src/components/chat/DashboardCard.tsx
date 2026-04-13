/**
 * DashboardCard.tsx
 *
 * Renders a DashboardPayload from the agent as a visual card.
 * Supports: full dashboard (charts + KPIs), quick_stat, and no_data.
 *
 * All types come from @/types/chat — never redefined here.
 */
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  LineChart,
  Line,
} from "recharts";
import { DashboardPayload, ChartConfig, KPI } from "@/types/chat";
import ReactMarkdown from "react-markdown";

// ── Color palette ─────────────────────────────────────────────────────────────

const COLORS = [
  "hsl(217,91%,60%)",
  "hsl(142,71%,45%)",
  "hsl(25,95%,53%)",
  "hsl(262,83%,58%)",
  "hsl(0,72%,51%)",
  "hsl(199,89%,48%)",
];

const STATUS_CLASSES: Record<string, string> = {
  critical: "bg-red-50 text-red-700 border-red-200",
  warning: "bg-yellow-50 text-yellow-700 border-yellow-200",
  good: "bg-green-50 text-green-700 border-green-200",
  ok: "bg-green-50 text-green-700 border-green-200",
  info: "bg-blue-50 text-blue-700 border-blue-200",
};

const TREND_ICON: Record<string, string> = { up: "↑", down: "↓", stable: "→" };

// ── KPI card ──────────────────────────────────────────────────────────────────

function KPICard({ kpi }: { kpi: KPI }) {
  const cls = STATUS_CLASSES[kpi.status ?? "info"] ?? STATUS_CLASSES.info;
  return (
    <div className="bg-muted/50 rounded-lg p-3 flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{kpi.label}</span>
      <div className="flex items-baseline gap-1.5">
        <span className="text-2xl font-medium">{kpi.value}</span>
        {kpi.trend && (
          <span className="text-xs text-muted-foreground">
            {TREND_ICON[kpi.trend] ?? ""}
          </span>
        )}
      </div>
      {kpi.status && (
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded border w-fit ${cls}`}
        >
          {kpi.status}
        </span>
      )}
      {kpi.context && (
        <span className="text-[11px] text-muted-foreground">{kpi.context}</span>
      )}
    </div>
  );
}

// ── Single chart ──────────────────────────────────────────────────────────────

function SingleChart({ chart }: { chart: ChartConfig }) {
  const xKey = chart.xKey ?? chart.nameKey ?? "label";
  const yKey = chart.yKey ?? chart.valueKey ?? "value";

  if (!chart.data || chart.data.length === 0) {
    return (
      <div className="bg-card border rounded-lg p-3">
        <p className="text-xs font-medium mb-1">{chart.title}</p>
        <p className="text-xs text-muted-foreground">
          No data available for this chart.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-card border rounded-lg p-3">
      <p className="text-xs font-medium mb-0.5">{chart.title}</p>
      {chart.insight && (
        <p className="text-[11px] text-muted-foreground mb-2">
          {chart.insight}
        </p>
      )}
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          {chart.type === "pie" || chart.type === "donut" ? (
            <PieChart>
              <Pie
                data={chart.data as Record<string, unknown>[]}
                dataKey={yKey}
                nameKey={xKey}
                cx="50%"
                cy="50%"
                outerRadius={chart.type === "donut" ? 70 : 70}
                innerRadius={chart.type === "donut" ? 35 : 0}
                label={({ name, percent }) =>
                  `${name} ${Math.round((percent as number) * 100)}%`
                }
                labelLine={false}
              >
                {(chart.data as Record<string, unknown>[]).map((_, i) => (
                  <Cell
                    key={i}
                    fill={chart.colors?.[i] ?? COLORS[i % COLORS.length]}
                  />
                ))}
              </Pie>
              <Tooltip />
            </PieChart>
          ) : chart.type === "line" || chart.type === "area" ? (
            <LineChart data={chart.data as Record<string, unknown>[]}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(0,0%,90%)" />
              <XAxis dataKey={xKey} tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Line
                type="monotone"
                dataKey={yKey}
                stroke={chart.colors?.[0] ?? COLORS[0]}
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          ) : (
            // bar / stackedBar (default)
            <BarChart data={chart.data as Record<string, unknown>[]}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(0,0%,90%)" />
              <XAxis dataKey={xKey} tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Bar dataKey={yKey} radius={[4, 4, 0, 0]}>
                {(chart.data as Record<string, unknown>[]).map((_, i) => (
                  <Cell
                    key={i}
                    fill={chart.colors?.[i] ?? COLORS[i % COLORS.length]}
                  />
                ))}
              </Bar>
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ── Quick stat card ───────────────────────────────────────────────────────────

function QuickStat({ payload }: { payload: DashboardPayload }) {
  return (
    <div className="bg-muted/50 rounded-lg p-4 inline-flex flex-col gap-1 min-w-[160px]">
      <span className="text-xs text-muted-foreground">{payload.label}</span>
      <span className="text-3xl font-semibold">{payload.value}</span>
      {payload.context && (
        <span className="text-[11px] text-muted-foreground">
          {payload.context}
        </span>
      )}
    </div>
  );
}

// ── Main DashboardCard ────────────────────────────────────────────────────────

export function DashboardCard({ dashboard }: { dashboard: DashboardPayload }) {
  // no_data shape
  if (dashboard.type === "no_data") {
    return (
      <div className="mt-2 text-sm text-muted-foreground border rounded-lg px-4 py-3">
        {dashboard.message ?? "No data available."}
      </div>
    );
  }
  if (dashboard.type === "clarification") {
    return (
      <div className="mt-2 rounded-2xl px-4 py-2.5 text-sm leading-relaxed bg-chat-assistant text-chat-assistant-fg border rounded-bl-md shadow-sm">
        <div className="prose-chat">
          <ReactMarkdown>{dashboard.message ?? ""}</ReactMarkdown>
        </div>
      </div>
    );
  }

  // quick_stat shape
  if (dashboard.type === "quick_stat") {
    return (
      <div className="mt-2">
        <QuickStat payload={dashboard} />
      </div>
    );
  }

  // Legacy single-chart fallback (old API shape)
  if (!dashboard.kpis && !dashboard.charts && dashboard.chartType) {
    return (
      <div className="mt-2">
        <SingleChart
          chart={{
            type: (dashboard.chartType as ChartConfig["type"]) ?? "bar",
            title: dashboard.title ?? "",
            data: (dashboard.data as Record<string, unknown>[]) ?? [],
          }}
        />
      </div>
    );
  }

  // Full dashboard shape
  const hasKpis = (dashboard.kpis?.length ?? 0) > 0;
  const hasCharts = (dashboard.charts?.length ?? 0) > 0;

  if (!hasKpis && !hasCharts) {
    return (
      <div className="mt-2 text-sm text-muted-foreground border rounded-lg px-4 py-3">
        Dashboard received but contains no charts or KPIs.
      </div>
    );
  }

  return (
    <div className="mt-2 space-y-3 w-full">
      {dashboard.title && (
        <h4 className="text-sm font-semibold text-foreground">
          {dashboard.title}
        </h4>
      )}

      {dashboard.summary && (
        <div className="border-l-2 border-blue-400 pl-3 text-xs text-muted-foreground leading-relaxed">
          {dashboard.summary}
        </div>
      )}

      {hasKpis && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {dashboard.kpis!.map((kpi, i) => (
            <KPICard key={i} kpi={kpi} />
          ))}
        </div>
      )}

      {hasCharts && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {dashboard.charts!.map((ch, i) => (
            <SingleChart key={i} chart={ch} />
          ))}
        </div>
      )}

      {dashboard.generated_at && (
        <p className="text-[10px] text-muted-foreground text-right">
          Generated {new Date(dashboard.generated_at).toLocaleTimeString()}
        </p>
      )}
    </div>
  );
}
