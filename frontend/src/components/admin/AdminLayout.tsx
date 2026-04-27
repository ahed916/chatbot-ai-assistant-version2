import { ReactNode } from "react";
import { AdminSidebar } from "./AdminSideBar";

interface AdminLayoutProps {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function AdminLayout({ title, description, actions, children }: AdminLayoutProps) {
  return (
    <div className="flex h-screen w-full bg-background overflow-hidden">
      <AdminSidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        <header className="h-14 border-b border-border bg-background/80 backdrop-blur flex items-center justify-between px-6 shrink-0">
          <div>
            <h1 className="text-base font-semibold text-foreground leading-tight">{title}</h1>
            {description && <p className="text-xs text-muted-foreground">{description}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-7xl px-6 py-6">{children}</div>
        </main>
      </div>
    </div>
  );
}