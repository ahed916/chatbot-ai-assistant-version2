/**
 * RiskAlertBell.tsx
 *
 * Notification bell that sits in the app header.
 * Shows a red badge with unread alert count.
 * Clicking opens a dropdown panel listing all alerts.
 * Clicking an alert opens a conversation for it.
 *
 * Props come from useChatState() return value:
 *   riskAlerts, openRiskAlert, markAlertRead
 */
import { useRef, useState, useEffect } from "react";
import { Bell } from "lucide-react";
import { RiskAlert } from "@/types/chat";

interface RiskAlertBellProps {
  alerts: RiskAlert[];
  onOpen: (alertId: string) => void; // opens the conversation
  onMarkRead: (alertId: string) => void;
}

const HEALTH_COLOR: Record<string, string> = {
  Critical: "text-red-600",
  "At Risk": "text-orange-500",
  "Needs Attention": "text-yellow-600",
  Healthy: "text-green-600",
  Unknown: "text-muted-foreground",
};

const HEALTH_BG: Record<string, string> = {
  Critical: "bg-red-50 border-red-200",
  "At Risk": "bg-orange-50 border-orange-200",
  "Needs Attention": "bg-yellow-50 border-yellow-200",
  Healthy: "bg-green-50 border-green-200",
  Unknown: "bg-muted border-border",
};

export function RiskAlertBell({
  alerts,
  onOpen,
  onMarkRead,
}: RiskAlertBellProps) {
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const unreadCount = alerts.filter((a) => !a.read).length;

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (
        panelRef.current &&
        !panelRef.current.contains(e.target as Node) &&
        buttonRef.current &&
        !buttonRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const handleAlertClick = (alert: RiskAlert) => {
    onOpen(alert.id);
    onMarkRead(alert.id);
    setOpen(false);
  };

  const markAllRead = () => {
    alerts.forEach((a) => {
      if (!a.read) onMarkRead(a.id);
    });
  };

  return (
    <div className="relative">
      {/* ── Bell button ─────────────────────────────────────────────────── */}
      <button
        ref={buttonRef}
        onClick={() => setOpen((v) => !v)}
        className="relative p-2 rounded-lg hover:bg-muted transition-colors"
        aria-label={`Risk alerts${
          unreadCount > 0 ? ` (${unreadCount} unread)` : ""
        }`}
      >
        <Bell className="h-5 w-5 text-muted-foreground" />

        {/* Red badge */}
        {unreadCount > 0 && (
          <span className="absolute top-1 right-1 flex h-4 w-4 items-center justify-center rounded-full bg-red-500 text-[10px] font-semibold text-white leading-none">
            {unreadCount > 9 ? "9+" : unreadCount}
          </span>
        )}
      </button>

      {/* ── Dropdown panel ───────────────────────────────────────────────── */}
      {open && (
        <div
          ref={panelRef}
          className="absolute right-0 top-full mt-2 w-80 rounded-xl border bg-background shadow-lg z-50 overflow-hidden"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3 border-b">
            <span className="text-sm font-medium">Risk Alerts</span>
            {unreadCount > 0 && (
              <button
                onClick={markAllRead}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors"
              >
                Mark all read
              </button>
            )}
          </div>

          {/* Alert list */}
          <div className="max-h-96 overflow-y-auto">
            {alerts.length === 0 ? (
              <div className="px-4 py-8 text-center">
                <Bell className="h-8 w-8 text-muted-foreground/40 mx-auto mb-2" />
                <p className="text-sm text-muted-foreground">No risk alerts</p>
                <p className="text-xs text-muted-foreground/60 mt-1">
                  Alerts appear when the risk scanner detects issues
                </p>
              </div>
            ) : (
              <ul className="divide-y">
                {alerts.map((alert) => (
                  <li key={alert.id}>
                    <button
                      onClick={() => handleAlertClick(alert)}
                      className={`w-full text-left px-4 py-3 hover:bg-muted/50 transition-colors ${
                        !alert.read ? "bg-muted/30" : ""
                      }`}
                    >
                      {/* Top row: health badge + unread dot */}
                      <div className="flex items-center gap-2 mb-1">
                        <span
                          className={`text-[11px] font-medium px-2 py-0.5 rounded-full border ${
                            HEALTH_BG[alert.overall_health] ?? HEALTH_BG.Unknown
                          } ${
                            HEALTH_COLOR[alert.overall_health] ??
                            HEALTH_COLOR.Unknown
                          }`}
                        >
                          {alert.overall_health}
                        </span>

                        {alert.critical_count > 0 && (
                          <span className="text-[11px] text-red-500 font-medium">
                            {alert.critical_count} critical
                          </span>
                        )}

                        {/* Unread indicator */}
                        {!alert.read && (
                          <span className="ml-auto h-2 w-2 rounded-full bg-red-500 shrink-0" />
                        )}
                      </div>

                      {/* Message preview */}
                      <p className="text-xs text-muted-foreground line-clamp-2 leading-relaxed">
                        {alert.message}
                      </p>

                      {/* Recommendations count */}
                      {alert.recommendations.length > 0 && (
                        <p className="text-[11px] text-muted-foreground/60 mt-1">
                          {alert.recommendations.length} recommended action
                          {alert.recommendations.length > 1 ? "s" : ""}
                        </p>
                      )}

                      <p className="text-[11px] text-blue-500 mt-1.5">
                        Click to open conversation →
                      </p>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Footer */}
          {alerts.length > 0 && (
            <div className="px-4 py-2 border-t bg-muted/30">
              <p className="text-[11px] text-muted-foreground text-center">
                Risk scans run every 2 hours automatically
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
