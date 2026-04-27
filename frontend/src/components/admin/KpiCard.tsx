import { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface KpiCardProps {
  label: string;
  value: string | number;
  icon: LucideIcon;
  trend?: { value: number; positive?: boolean };
  accent?: "primary" | "emerald" | "amber" | "sky";
  hint?: string;
}

const ACCENTS: Record<NonNullable<KpiCardProps["accent"]>, string> = {
  primary: "bg-primary/10 text-primary",
  emerald: "bg-emerald-500/10 text-emerald-600",
  amber: "bg-amber-500/10 text-amber-600",
  sky: "bg-sky-500/10 text-sky-600",
};

export function KpiCard({ label, value, icon: Icon, trend, accent = "primary", hint }: KpiCardProps) {
  return (
    <div className="group relative overflow-hidden rounded-xl border bg-card p-5 transition-all hover:shadow-md hover:-translate-y-0.5">
      <div className="flex items-start justify-between">
        <div className="space-y-1">
          <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{label}</p>
          <p className="text-3xl font-bold text-foreground tabular-nums">{value}</p>
          {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
        </div>
        <div className={cn("flex h-10 w-10 items-center justify-center rounded-lg", ACCENTS[accent])}>
          <Icon className="h-5 w-5" />
        </div>
      </div>
      {trend && (
        <div className="mt-3 flex items-center gap-1 text-xs">
          <span className={cn("font-medium", trend.positive ? "text-emerald-600" : "text-destructive")}>
            {trend.positive ? "▲" : "▼"} {Math.abs(trend.value)}%
          </span>
          <span className="text-muted-foreground">vs last period</span>
        </div>
      )}
    </div>
  );
}