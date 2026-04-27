import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { LayoutDashboard, Users, MessageSquare, LogOut, Activity } from "lucide-react";
import { supabase } from "@/lib/supabase";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/admin",               label: "Overview",      icon: LayoutDashboard, end: true },
  { to: "/admin/users",         label: "Users",         icon: Users },
  { to: "/admin/conversations", label: "Conversations", icon: MessageSquare },
  { to: "/admin/redmine",       label: "Redmine Stats", icon: Activity },
];

export function AdminSidebar() {
  const location = useLocation();
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => {
      setEmail(data.user?.email ?? null);
    });
  }, []);

  const handleLogout = async () => {
    await supabase.auth.signOut();
    window.location.href = "/login";
  };

  return (
    <aside className="hidden md:flex h-screen w-64 shrink-0 flex-col bg-sidebar-bg text-sidebar-fg border-r border-sidebar-border">
      {/* Brand */}
      <div className="flex items-center gap-2.5 px-5 h-14 border-b border-sidebar-border/60">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary shadow-md shadow-primary/20">
          <span className="font-bold text-primary-foreground text-sm">R</span>
        </div>
        <div className="flex flex-col leading-tight">
          <span className="font-semibold text-sm text-white">RedMind</span>
          <span className="text-[10px] uppercase tracking-wider text-sidebar-muted">Admin Console</span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1 sidebar-scroll overflow-y-auto">
        {NAV.map(({ to, label, icon: Icon, end }) => {
          const active = end ? location.pathname === to : location.pathname.startsWith(to);
          return (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={cn(
                "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-sidebar-active text-white"
                  : "text-sidebar-fg hover:bg-sidebar-hover hover:text-white",
              )}
            >
              <Icon
                className={cn(
                  "h-4 w-4",
                  active ? "text-primary" : "text-sidebar-muted group-hover:text-white",
                )}
              />
              <span>{label}</span>
            </NavLink>
          );
        })}
      </nav>

      {/* User footer */}
      <div className="border-t border-sidebar-border/60 p-3">
        <div className="flex items-center gap-3 rounded-lg px-2 py-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/20 text-primary text-xs font-semibold">
            {email?.[0]?.toUpperCase() ?? "A"}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-xs font-medium text-white truncate">{email ?? "—"}</p>
            <p className="text-[10px] uppercase tracking-wider text-sidebar-muted">Administrator</p>
          </div>
          <button
            onClick={handleLogout}
            title="Logout"
            className="text-sidebar-muted hover:text-white transition-colors p-1.5 rounded-md hover:bg-sidebar-hover"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </aside>
  );
}