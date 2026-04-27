import { useEffect, useState } from "react";
import { Bug, FolderKanban, AlertTriangle, RefreshCw } from "lucide-react";
import { adminApi, RedmineStats } from "@/lib/adminApi";
import { AdminLayout } from "@/components/admin/AdminLayout";
import { KpiCard } from "@/components/admin/KpiCard";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  PieChart, Pie, Cell, Legend,
} from "recharts";

const COLORS = [
  "hsl(0, 72%, 51%)",
  "hsl(25, 95%, 53%)",
  "hsl(45, 93%, 47%)",
  "hsl(142, 71%, 45%)",
  "hsl(199, 89%, 48%)",
  "hsl(262, 83%, 58%)",
];

export default function RedmineStatsPage() {
  const [stats, setStats] = useState<RedmineStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // api interceptor attaches the Supabase token automatically
  const load = () => {
    setLoading(true);
    setError(null);
    adminApi.getRedmineStats()
      .then(setStats)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const status   = stats ? Object.entries(stats.by_status).map(([name, value]) => ({ name, value })) : [];
  const tracker  = stats ? Object.entries(stats.by_tracker).map(([name, value]) => ({ name, value })) : [];
  const projects = stats
    ? Object.entries(stats.by_project)
        .sort((a, b) => b[1] - a[1])
        .map(([name, value]) => ({ name, value }))
    : [];

  return (
    <AdminLayout
      title="Redmine Statistics"
      description="Global activity pulled from your Redmine instance"
      actions={
        <Button size="sm" variant="outline" onClick={load} disabled={loading}>
          <RefreshCw className={`h-4 w-4 mr-1.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      }
    >
      {error ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-6 text-sm text-destructive">
          <p className="font-medium mb-1">Could not load Redmine stats</p>
          <p className="text-xs">{error}</p>
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            {loading ? (
              Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)
            ) : (
              <>
                <KpiCard label="Total Issues"    value={stats?.total_issues   ?? 0} icon={Bug}          accent="primary" />
                <KpiCard label="Active Projects" value={stats?.total_projects ?? 0} icon={FolderKanban} accent="emerald" />
                <KpiCard label="Overdue"         value={stats?.overdue        ?? 0} icon={AlertTriangle} accent="amber" hint="Past due date" />
              </>
            )}
          </div>

          <div className="mt-6 grid gap-4 lg:grid-cols-2">
            {/* By status */}
            <div className="rounded-xl border bg-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-1">By status</h3>
              <p className="text-xs text-muted-foreground mb-3">Distribution across workflow states</p>
              <div className="h-72">
                {loading ? <Skeleton className="h-full w-full" /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={status}
                        dataKey="value"
                        nameKey="name"
                        cx="50%"
                        cy="45%"
                        innerRadius={50}
                        outerRadius={90}
                        paddingAngle={2}
                      >
                        {status.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
                      </Pie>
                      <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                      <Legend wrapperStyle={{ fontSize: 11 }} iconSize={8} />
                    </PieChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>

            {/* By tracker */}
            <div className="rounded-xl border bg-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-1">By tracker</h3>
              <p className="text-xs text-muted-foreground mb-3">Issue type breakdown</p>
              <div className="h-72">
                {loading ? <Skeleton className="h-full w-full" /> : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={tracker} margin={{ left: -10, right: 5, top: 5, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="hsl(0,0%,92%)" />
                      <XAxis dataKey="name" tick={{ fontSize: 12 }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fontSize: 12 }} axisLine={false} tickLine={false} />
                      <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                      <Bar dataKey="value" fill="hsl(0, 72%, 51%)" radius={[6, 6, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>
          </div>

          {/* By project */}
          <div className="mt-4 rounded-xl border bg-card p-5">
            <h3 className="text-sm font-semibold text-foreground mb-1">By project</h3>
            <p className="text-xs text-muted-foreground mb-3">All projects ranked by issue volume</p>
            <div className="h-80">
              {loading ? <Skeleton className="h-full w-full" /> : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={projects}
                    layout="vertical"
                    margin={{ left: 10, right: 20, top: 5, bottom: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(0,0%,92%)" horizontal={false} />
                    <XAxis type="number" tick={{ fontSize: 12 }} axisLine={false} tickLine={false} />
                    <YAxis
                      type="category"
                      dataKey="name"
                      tick={{ fontSize: 12 }}
                      axisLine={false}
                      tickLine={false}
                      width={120}
                    />
                    <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                    <Bar dataKey="value" fill="hsl(25, 95%, 53%)" radius={[0, 6, 6, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        </>
      )}
    </AdminLayout>
  );
}