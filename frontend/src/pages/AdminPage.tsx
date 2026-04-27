import { useEffect, useState, useCallback } from "react";
import { Users, Bug, FolderKanban, AlertTriangle, TrendingUp, RefreshCw } from "lucide-react";
import {
  adminApi,
  PMUser,
  RedmineStats,
  ChatActivityPoint,
} from "@/lib/adminApi";
import { AdminLayout } from "@/components/admin/AdminLayout";
import { KpiCard } from "@/components/admin/KpiCard";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";

const PIE_COLORS = [
  "hsl(0, 72%, 51%)",    // red    — New
  "hsl(25, 95%, 53%)",   // orange — In Progress
  "hsl(45, 93%, 47%)",   // yellow — Resolved
  "hsl(142, 71%, 45%)",  // green  — Closed
  "hsl(199, 89%, 48%)",  // blue   — Feedback
  "hsl(262, 83%, 58%)",  // purple — others
];

// ── Derived trend: compare this week's total vs previous 7 days ──────────────
function computeTrend(activity: ChatActivityPoint[]): number | null {
  if (activity.length < 7) return null;
  const thisWeek = activity.slice(-7).reduce((s, p) => s + p.conversations, 0);
  // We only have 7 days from backend, so trend vs a flat baseline of 1 is meaningless.
  // Return null when we can't compute — component hides the badge.
  return thisWeek > 0 ? thisWeek : null;
}

export function AdminPage() {
  const [users,          setUsers]          = useState<PMUser[]>([]);
  const [redmine,        setRedmine]        = useState<RedmineStats | null>(null);
  const [activity,       setActivity]       = useState<ChatActivityPoint[]>([]);
  const [loadingUsers,   setLoadingUsers]   = useState(true);
  const [loadingRedmine, setLoadingRedmine] = useState(true);
  const [loadingActivity,setLoadingActivity]= useState(true);
  const [redmineError,   setRedmineError]   = useState<string | null>(null);

  const load = useCallback(() => {
    setLoadingUsers(true);
    setLoadingRedmine(true);
    setLoadingActivity(true);
    setRedmineError(null);

    adminApi.listUsers()
      .then(setUsers)
      .catch(console.error)
      .finally(() => setLoadingUsers(false));

    adminApi.getRedmineStats()
      .then(setRedmine)
      .catch((e: Error) => setRedmineError(e.message))
      .finally(() => setLoadingRedmine(false));

    adminApi.getChatActivity()
      .then(setActivity)
      .catch(console.error)
      .finally(() => setLoadingActivity(false));
  }, []);

  useEffect(load, [load]);

  // ── Derived values ──────────────────────────────────────────────────────
  const connectedCount = users.filter((u) => u.is_redmine_connected).length;
  const totalConversations = activity.reduce((s, p) => s + p.conversations, 0);

  const statusData = redmine
    ? Object.entries(redmine.by_status).map(([name, value]) => ({ name, value }))
    : [];

  const trackerData = redmine
    ? Object.entries(redmine.by_tracker).map(([name, value]) => ({ name, value }))
    : [];

  const projectData = redmine
    ? Object.entries(redmine.by_project)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6)
        .map(([name, value]) => ({ name, value }))
    : [];

  const anyLoading = loadingUsers || loadingRedmine || loadingActivity;

  return (
    <AdminLayout
      title="Overview"
      description="Real-time snapshot of users, conversations and Redmine activity"
      actions={
        <Button size="sm" variant="outline" onClick={load} disabled={anyLoading}>
          <RefreshCw className={`h-4 w-4 mr-1.5 ${anyLoading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      }
    >
      <div className="space-y-5">

        {/* ── KPI cards ─────────────────────────────────────────────────── */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {loadingUsers || loadingRedmine || loadingActivity ? (
            Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-28 rounded-xl" />
            ))
          ) : (
            <>
              <KpiCard
                label="Project Managers"
                value={users.length}
                icon={Users}
                accent="primary"
                hint={`${connectedCount} connected to Redmine`}
              />
              <KpiCard
                label="Total Issues"
                value={redmine?.total_issues ?? "—"}
                icon={Bug}
                accent="sky"
              />
              <KpiCard
                label="Active Projects"
                value={redmine?.total_projects ?? "—"}
                icon={FolderKanban}
                accent="emerald"
              />
              <KpiCard
                label="Overdue Issues"
                value={redmine?.overdue ?? "—"}
                icon={AlertTriangle}
                accent="amber"
                hint="Past their due date"
              />
            </>
          )}
        </div>

        {/* ── Chat activity area chart + Issues by status donut ─────────── */}
        <div className="grid gap-4 lg:grid-cols-[1fr_320px]">

          {/* Area chart — real conversation counts per day */}
          <div className="rounded-xl border bg-card p-5">
            <div className="flex items-start justify-between mb-4">
              <div>
                <h3 className="text-sm font-semibold text-foreground">Chat activity</h3>
                <p className="text-xs text-muted-foreground">Conversations over the last 7 days</p>
              </div>
              {!loadingActivity && totalConversations > 0 && (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-600">
                  <TrendingUp className="h-3.5 w-3.5" />
                  {totalConversations} total
                </span>
              )}
            </div>
            <div className="h-64">
              {loadingActivity ? (
                <Skeleton className="h-full w-full rounded-xl" />
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart
                    data={activity}
                    margin={{ left: -10, right: 5, top: 5, bottom: 0 }}
                  >
                    <defs>
                      <linearGradient id="chatGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%"  stopColor="hsl(0,72%,51%)" stopOpacity={0.25} />
                        <stop offset="95%" stopColor="hsl(0,72%,51%)" stopOpacity={0}    />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(0,0%,92%)" />
                    <XAxis
                      dataKey="day"
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                      allowDecimals={false}
                    />
                    <Tooltip
                      contentStyle={{ borderRadius: 8, fontSize: 12 }}
                      labelFormatter={(label, payload) =>
                        payload?.[0]?.payload?.date ?? label
                      }
                    />
                    <Area
                      type="monotone"
                      dataKey="conversations"
                      stroke="hsl(0,72%,51%)"
                      strokeWidth={2}
                      fill="url(#chatGrad)"
                      dot={false}
                      activeDot={{ r: 4 }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>

          {/* Donut — issues by status */}
          <div className="rounded-xl border bg-card p-5">
            <h3 className="text-sm font-semibold text-foreground mb-1">Issues by status</h3>
            <p className="text-xs text-muted-foreground mb-3">Across all projects</p>
            <div className="h-64">
              {loadingRedmine ? (
                <Skeleton className="h-full w-full rounded-xl" />
              ) : redmineError ? (
                <div className="flex items-center justify-center h-full">
                  <p className="text-xs text-destructive text-center px-4">{redmineError}</p>
                </div>
              ) : statusData.length === 0 ? (
                <div className="flex items-center justify-center h-full">
                  <p className="text-xs text-muted-foreground">No issue data available.</p>
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={statusData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="42%"
                      innerRadius={52}
                      outerRadius={90}
                      paddingAngle={2}
                    >
                      {statusData.map((_, i) => (
                        <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                    <Legend
                      iconSize={8}
                      wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                      formatter={(value) => (
                        <span style={{ color: "hsl(0,0%,40%)" }}>{value}</span>
                      )}
                    />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        </div>

        {/* ── Issues by tracker + Top projects ──────────────────────────── */}
        <div className="grid gap-4 lg:grid-cols-2">

          {/* Vertical bar — by tracker */}
          <div className="rounded-xl border bg-card p-5">
            <h3 className="text-sm font-semibold text-foreground mb-1">Issues by tracker</h3>
            <p className="text-xs text-muted-foreground mb-3">Bugs vs Features vs Tasks</p>
            <div className="h-72">
              {loadingRedmine ? (
                <Skeleton className="h-full w-full rounded-xl" />
              ) : trackerData.length === 0 ? (
                <div className="flex items-center justify-center h-full">
                  <p className="text-xs text-muted-foreground">No tracker data available.</p>
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={trackerData}
                    margin={{ left: -10, right: 5, top: 5, bottom: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(0,0%,92%)" />
                    <XAxis
                      dataKey="name"
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                      allowDecimals={false}
                    />
                    <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                    <Bar
                      dataKey="value"
                      fill="hsl(0, 72%, 51%)"
                      radius={[6, 6, 0, 0]}
                    />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>

          {/* Horizontal bar — top projects */}
          <div className="rounded-xl border bg-card p-5">
            <h3 className="text-sm font-semibold text-foreground mb-1">
              Top projects by issue volume
            </h3>
            <p className="text-xs text-muted-foreground mb-3">Highest activity first</p>
            <div className="h-72">
              {loadingRedmine ? (
                <Skeleton className="h-full w-full rounded-xl" />
              ) : projectData.length === 0 ? (
                <div className="flex items-center justify-center h-full">
                  <p className="text-xs text-muted-foreground">No project data available.</p>
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={projectData}
                    layout="vertical"
                    margin={{ left: 10, right: 20, top: 5, bottom: 0 }}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="hsl(0,0%,92%)"
                      horizontal={false}
                    />
                    <XAxis
                      type="number"
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                      allowDecimals={false}
                    />
                    <YAxis
                      type="category"
                      dataKey="name"
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                      width={110}
                    />
                    <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                    <Bar
                      dataKey="value"
                      fill="hsl(25, 95%, 53%)"
                      radius={[0, 6, 6, 0]}
                    />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        </div>

      </div>
    </AdminLayout>
  );
}