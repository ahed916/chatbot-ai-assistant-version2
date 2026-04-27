/**
 * AdminDashboard.tsx
 *
 * Full-featured admin panel with:
 *  - Sidebar navigation
 *  - Project Manager user table (paginated)
 *  - Chat log viewer (conversations + messages per PM)
 *  - Redmine overview dashboard (stats & charts via admin Redmine key)
 *  - Create / delete PM accounts
 */

import { useState, useEffect } from "react";
import {
  Users, MessageSquare, BarChart2, Plus, Trash2,
  Wifi, WifiOff, LogOut, ChevronRight, ChevronLeft,
  AlertCircle, CheckCircle2, Loader2, Eye, X, ArrowLeft,
} from "lucide-react";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "../components/ui/table";
import { api } from "../api/client";

// ─── Types ────────────────────────────────────────────────────────────────────

interface User {
  id: string;
  email: string;
  full_name: string;
  is_redmine_connected: boolean;
  created_at?: string;
}

interface Conversation {
  id: string;
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

interface Message {
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

interface RedmineStats {
  total_issues: number;
  total_projects: number;
  overdue: number;
  by_status: Record<string, number>;
  by_tracker: Record<string, number>;
  by_project: Record<string, number>;
}

interface AdminDashboardProps {
  users: User[];
  onCreateUser: (data: { email: string; password: string; full_name: string }) => void;
  onDeleteUser: (id: string) => void;
  isCreating: boolean;
  error: string | null;
  onRefreshUsers: () => void;
}

// ─── Sidebar nav items ────────────────────────────────────────────────────────

type NavTab = "users" | "chatlogs" | "stats";

const NAV_ITEMS: { id: NavTab; label: string; icon: React.ReactNode }[] = [
  { id: "users",    label: "Project Managers", icon: <Users size={16} /> },
  { id: "chatlogs", label: "Chat Logs",         icon: <MessageSquare size={16} /> },
  { id: "stats",    label: "Redmine Overview",  icon: <BarChart2 size={16} /> },
];

// ─── Small helpers ────────────────────────────────────────────────────────────

function Badge({ connected }: { connected: boolean }) {
  return connected ? (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      fontSize: 12, padding: "2px 8px",
      borderRadius: "var(--border-radius-md)",
      background: "var(--color-background-success)",
      color: "var(--color-text-success)",
    }}>
      <Wifi size={11} /> Connected
    </span>
  ) : (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      fontSize: 12, padding: "2px 8px",
      borderRadius: "var(--border-radius-md)",
      background: "var(--color-background-secondary)",
      color: "var(--color-text-secondary)",
    }}>
      <WifiOff size={11} /> Not connected
    </span>
  );
}

function StatCard({ label, value, sub }: { label: string; value: number | string; sub?: string }) {
  return (
    <div style={{
      background: "var(--color-background-secondary)",
      borderRadius: "var(--border-radius-md)",
      padding: "1rem",
    }}>
      <p style={{ fontSize: 13, color: "var(--color-text-secondary)", margin: "0 0 4px" }}>{label}</p>
      <p style={{ fontSize: 28, fontWeight: 500, margin: 0, color: "var(--color-text-primary)" }}>{value}</p>
      {sub && <p style={{ fontSize: 12, color: "var(--color-text-secondary)", margin: "4px 0 0" }}>{sub}</p>}
    </div>
  );
}

// ─── Horizontal bar chart ─────────────────────────────────────────────────────

function HBar({ data, title, colorVar }: { data: Record<string, number>; title: string; colorVar: string }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (!entries.length) return null;
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div style={{
      background: "var(--color-background-primary)",
      border: "0.5px solid var(--color-border-tertiary)",
      borderRadius: "var(--border-radius-lg)",
      padding: "1.25rem",
    }}>
      <p style={{ fontSize: 13, fontWeight: 500, margin: "0 0 1rem", color: "var(--color-text-secondary)" }}>{title}</p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {entries.map(([key, val]) => (
          <div key={key} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 12, color: "var(--color-text-secondary)", minWidth: 90, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{key}</span>
            <div style={{ flex: 1, background: "var(--color-background-secondary)", borderRadius: 4, height: 8, overflow: "hidden" }}>
              <div style={{ width: `${(val / max) * 100}%`, height: "100%", background: colorVar, borderRadius: 4, transition: "width 0.4s ease" }} />
            </div>
            <span style={{ fontSize: 12, fontWeight: 500, minWidth: 24, textAlign: "right", color: "var(--color-text-primary)" }}>{val}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Pagination ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 8;

function usePagination<T>(items: T[]) {
  const [page, setPage] = useState(1);
  const total = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const paged = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  return { paged, page, total, setPage };
}

// ─── Tab: Users ───────────────────────────────────────────────────────────────

function UsersTab({
  users, onCreateUser, onDeleteUser, isCreating, error,
}: {
  users: User[];
  onCreateUser: (d: { email: string; password: string; full_name: string }) => void;
  onDeleteUser: (id: string) => void;
  isCreating: boolean;
  error: string | null;
}) {
  const [showForm, setShowForm] = useState(false);
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { paged, page, total, setPage } = usePagination(users);

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    onCreateUser({ email, password, full_name: fullName });
    setFullName(""); setEmail(""); setPassword("");
    setShowForm(false);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>

      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0, color: "var(--color-text-primary)" }}>
            Project Managers
          </h2>
          <p style={{ fontSize: 13, color: "var(--color-text-secondary)", margin: "2px 0 0" }}>
            {users.length} account{users.length !== 1 ? "s" : ""}
          </p>
        </div>
        <Button size="sm" onClick={() => setShowForm(v => !v)}>
          <Plus size={14} style={{ marginRight: 6 }} />
          {showForm ? "Cancel" : "Add manager"}
        </Button>
      </div>

      {/* Create form */}
      {showForm && (
        <div style={{
          background: "var(--color-background-primary)",
          border: "0.5px solid var(--color-border-secondary)",
          borderRadius: "var(--border-radius-lg)",
          padding: "1.25rem",
        }}>
          <p style={{ fontSize: 14, fontWeight: 500, margin: "0 0 1rem", color: "var(--color-text-primary)" }}>
            New project manager
          </p>
          <form onSubmit={handleCreate} style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Full name</label>
              <Input value={fullName} onChange={e => setFullName(e.target.value)} required disabled={isCreating} placeholder="Jane Smith" />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Email</label>
              <Input type="email" value={email} onChange={e => setEmail(e.target.value)} required disabled={isCreating} placeholder="jane@company.com" />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4, gridColumn: "1 / -1" }}>
              <label style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Password</label>
              <Input type="password" value={password} onChange={e => setPassword(e.target.value)} required disabled={isCreating} placeholder="••••••••" />
            </div>
            {error && (
              <div style={{
                gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: 6,
                fontSize: 13, color: "var(--color-text-danger)",
                padding: "8px 12px",
                background: "var(--color-background-danger)",
                borderRadius: "var(--border-radius-md)",
              }}>
                <AlertCircle size={14} />{error}
              </div>
            )}
            <div style={{ gridColumn: "1 / -1" }}>
              <Button type="submit" disabled={isCreating}>
                {isCreating ? <><Loader2 size={14} className="animate-spin" style={{ marginRight: 6 }} />Creating…</> : "Create account"}
              </Button>
            </div>
          </form>
        </div>
      )}

      {/* Table */}
      <div style={{
        background: "var(--color-background-primary)",
        border: "0.5px solid var(--color-border-tertiary)",
        borderRadius: "var(--border-radius-lg)",
        overflow: "hidden",
      }}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Redmine</TableHead>
              <TableHead>Joined</TableHead>
              <TableHead style={{ textAlign: "right" }}>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {paged.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} style={{ textAlign: "center", color: "var(--color-text-secondary)", padding: "2rem" }}>
                  No project managers yet.
                </TableCell>
              </TableRow>
            )}
            {paged.map(user => (
              <TableRow key={user.id}>
                <TableCell style={{ fontWeight: 500 }}>{user.full_name}</TableCell>
                <TableCell style={{ color: "var(--color-text-secondary)", fontSize: 13 }}>{user.email}</TableCell>
                <TableCell><Badge connected={user.is_redmine_connected} /></TableCell>
                <TableCell style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>
                  {user.created_at ? new Date(user.created_at).toLocaleDateString() : "—"}
                </TableCell>
                <TableCell style={{ textAlign: "right" }}>
                  <button
                    onClick={() => {
                      if (confirm(`Delete ${user.full_name}?`)) onDeleteUser(user.id);
                    }}
                    style={{
                      background: "none", border: "none", cursor: "pointer",
                      color: "var(--color-text-danger)", padding: "4px 8px",
                      borderRadius: "var(--border-radius-md)",
                      display: "inline-flex", alignItems: "center",
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {/* Pagination */}
        {total > 1 && (
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "12px 16px",
            borderTop: "0.5px solid var(--color-border-tertiary)",
          }}>
            <span style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>
              Page {page} of {total}
            </span>
            <div style={{ display: "flex", gap: 4 }}>
              <Button size="sm" variant="ghost" disabled={page === 1} onClick={() => setPage(p => p - 1)}>
                <ChevronLeft size={14} />
              </Button>
              <Button size="sm" variant="ghost" disabled={page === total} onClick={() => setPage(p => p + 1)}>
                <ChevronRight size={14} />
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Tab: Chat Logs ───────────────────────────────────────────────────────────

function ChatLogsTab({ users }: { users: User[] }) {
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loadingConvs, setLoadingConvs] = useState(false);
  const [selectedConvId, setSelectedConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadingMsgs, setLoadingMsgs] = useState(false);
  const { paged, page, total, setPage } = usePagination(conversations);

  const selectedUser = users.find(u => u.id === selectedUserId);

  const loadConversations = async (userId: string) => {
    setLoadingConvs(true);
    setConversations([]);
    setSelectedConvId(null);
    setMessages([]);
    try {
      const res = await api.get(`/admin/users/${userId}/conversations`);
      setConversations(res.data);
    } catch {
      setConversations([]);
    } finally {
      setLoadingConvs(false);
    }
  };

  const loadMessages = async (convId: string) => {
    setLoadingMsgs(true);
    setMessages([]);
    try {
      const res = await api.get(`/admin/conversations/${convId}/messages`);
      setMessages(res.data);
    } catch {
      setMessages([]);
    } finally {
      setLoadingMsgs(false);
    }
  };

  const selectUser = (id: string) => {
    setSelectedUserId(id);
    loadConversations(id);
  };

  // Message view
  if (selectedConvId) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem", height: "100%" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            onClick={() => { setSelectedConvId(null); setMessages([]); }}
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--color-text-secondary)", display: "inline-flex", alignItems: "center", gap: 4, fontSize: 13 }}
          >
            <ArrowLeft size={14} /> Back to conversations
          </button>
        </div>
        <h2 style={{ fontSize: 16, fontWeight: 500, margin: 0 }}>
          {conversations.find(c => c.id === selectedConvId)?.title || "Conversation"}
        </h2>

        {loadingMsgs ? (
          <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--color-text-secondary)", fontSize: 14 }}>
            <Loader2 size={16} className="animate-spin" /> Loading messages…
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {messages.map((msg, i) => (
              <div key={i} style={{
                display: "flex",
                justifyContent: msg.role === "user" ? "flex-end" : "flex-start",
              }}>
                <div style={{
                  maxWidth: "75%",
                  padding: "10px 14px",
                  borderRadius: "var(--border-radius-lg)",
                  fontSize: 14,
                  lineHeight: 1.6,
                  background: msg.role === "user"
                    ? "var(--color-background-info)"
                    : "var(--color-background-secondary)",
                  color: msg.role === "user"
                    ? "var(--color-text-info)"
                    : "var(--color-text-primary)",
                }}>
                  <p style={{ margin: "0 0 4px", fontSize: 11, fontWeight: 500, opacity: 0.6 }}>
                    {msg.role === "user" ? "Project Manager" : "RedMind AI"}
                    {" · "}
                    {new Date(msg.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                  </p>
                  <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{msg.content}</p>
                </div>
              </div>
            ))}
            {messages.length === 0 && (
              <p style={{ color: "var(--color-text-secondary)", fontSize: 14 }}>No messages found.</p>
            )}
          </div>
        )}
      </div>
    );
  }

  // Conversations list
  if (selectedUserId) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            onClick={() => { setSelectedUserId(null); setConversations([]); }}
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--color-text-secondary)", display: "inline-flex", alignItems: "center", gap: 4, fontSize: 13 }}
          >
            <ArrowLeft size={14} /> All managers
          </button>
        </div>

        <div>
          <h2 style={{ fontSize: 18, fontWeight: 500, margin: "0 0 2px", color: "var(--color-text-primary)" }}>
            {selectedUser?.full_name}
          </h2>
          <p style={{ fontSize: 13, color: "var(--color-text-secondary)", margin: 0 }}>
            {conversations.length} conversation{conversations.length !== 1 ? "s" : ""}
          </p>
        </div>

        {loadingConvs ? (
          <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--color-text-secondary)", fontSize: 14 }}>
            <Loader2 size={16} className="animate-spin" /> Loading conversations…
          </div>
        ) : (
          <div style={{
            background: "var(--color-background-primary)",
            border: "0.5px solid var(--color-border-tertiary)",
            borderRadius: "var(--border-radius-lg)",
            overflow: "hidden",
          }}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Title</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Last activity</TableHead>
                  <TableHead style={{ textAlign: "right" }}>View</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {paged.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={4} style={{ textAlign: "center", color: "var(--color-text-secondary)", padding: "2rem" }}>
                      No conversations yet.
                    </TableCell>
                  </TableRow>
                )}
                {paged.map(conv => (
                  <TableRow key={conv.id}>
                    <TableCell style={{ fontWeight: 500, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {conv.title || "Untitled"}
                    </TableCell>
                    <TableCell style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>
                      {new Date(conv.created_at).toLocaleDateString()}
                    </TableCell>
                    <TableCell style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>
                      {new Date(conv.updated_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}
                    </TableCell>
                    <TableCell style={{ textAlign: "right" }}>
                      <button
                        onClick={() => { setSelectedConvId(conv.id); loadMessages(conv.id); }}
                        style={{
                          background: "none", border: "none", cursor: "pointer",
                          color: "var(--color-text-info)", padding: "4px 8px",
                          borderRadius: "var(--border-radius-md)",
                          display: "inline-flex", alignItems: "center", gap: 4, fontSize: 13,
                        }}
                      >
                        <Eye size={13} /> View
                      </button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {total > 1 && (
              <div style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "12px 16px", borderTop: "0.5px solid var(--color-border-tertiary)",
              }}>
                <span style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Page {page} of {total}</span>
                <div style={{ display: "flex", gap: 4 }}>
                  <Button size="sm" variant="ghost" disabled={page === 1} onClick={() => setPage(p => p - 1)}><ChevronLeft size={14} /></Button>
                  <Button size="sm" variant="ghost" disabled={page === total} onClick={() => setPage(p => p + 1)}><ChevronRight size={14} /></Button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // User picker
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      <div>
        <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0, color: "var(--color-text-primary)" }}>Chat Logs</h2>
        <p style={{ fontSize: 13, color: "var(--color-text-secondary)", margin: "2px 0 0" }}>
          Select a project manager to browse their conversations.
        </p>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {users.length === 0 && (
          <p style={{ fontSize: 14, color: "var(--color-text-secondary)" }}>No project managers found.</p>
        )}
        {users.map(user => (
          <button
            key={user.id}
            onClick={() => selectUser(user.id)}
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "14px 16px",
              background: "var(--color-background-primary)",
              border: "0.5px solid var(--color-border-tertiary)",
              borderRadius: "var(--border-radius-lg)",
              cursor: "pointer", textAlign: "left", width: "100%",
              transition: "border-color 0.15s",
            }}
            onMouseEnter={e => (e.currentTarget.style.borderColor = "var(--color-border-primary)")}
            onMouseLeave={e => (e.currentTarget.style.borderColor = "var(--color-border-tertiary)")}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <div style={{
                width: 36, height: 36, borderRadius: "50%",
                background: "var(--color-background-info)",
                color: "var(--color-text-info)",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 13, fontWeight: 500,
              }}>
                {user.full_name.split(" ").map(n => n[0]).join("").slice(0, 2).toUpperCase()}
              </div>
              <div>
                <p style={{ margin: 0, fontWeight: 500, fontSize: 14, color: "var(--color-text-primary)" }}>{user.full_name}</p>
                <p style={{ margin: 0, fontSize: 12, color: "var(--color-text-secondary)" }}>{user.email}</p>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Badge connected={user.is_redmine_connected} />
              <ChevronRight size={16} color="var(--color-text-secondary)" />
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ─── Tab: Redmine Stats ───────────────────────────────────────────────────────

function StatsTab() {
  const [stats, setStats] = useState<RedmineStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchStats = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.get("/admin/redmine-stats");
        setStats(res.data);
      } catch (e: any) {
        setError(e.response?.data?.detail ?? "Failed to load Redmine statistics.");
      } finally {
        setLoading(false);
      }
    };
    fetchStats();
  }, []);

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--color-text-secondary)", fontSize: 14, paddingTop: 24 }}>
        <Loader2 size={16} className="animate-spin" /> Loading Redmine data…
      </div>
    );
  }

  if (error) {
    return (
      <div style={{
        display: "flex", alignItems: "center", gap: 8, fontSize: 14,
        padding: "12px 16px",
        background: "var(--color-background-danger)",
        color: "var(--color-text-danger)",
        borderRadius: "var(--border-radius-md)",
      }}>
        <AlertCircle size={16} />{error}
      </div>
    );
  }

  if (!stats) return null;

  const healthColor = stats.overdue === 0
    ? "var(--color-text-success)"
    : stats.overdue > 5 ? "var(--color-text-danger)" : "var(--color-text-warning)";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      <div>
        <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0, color: "var(--color-text-primary)" }}>
          Redmine Overview
        </h2>
        <p style={{ fontSize: 13, color: "var(--color-text-secondary)", margin: "2px 0 0" }}>
          Global statistics across all projects
        </p>
      </div>

      {/* KPI cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <StatCard label="Total issues" value={stats.total_issues} />
        <StatCard label="Projects" value={stats.total_projects} />
        <StatCard
          label="Overdue issues"
          value={stats.overdue}
          sub={stats.overdue === 0 ? "All on track" : `${stats.overdue} past due date`}
        />
        <StatCard
          label="Health"
          value={stats.overdue === 0 ? "Good" : stats.overdue > 5 ? "At risk" : "Warning"}
        />
      </div>

      {/* Charts row */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <HBar data={stats.by_status} title="Issues by status" colorVar="var(--color-background-info)" />
        <HBar data={stats.by_tracker} title="Issues by type" colorVar="var(--color-background-success)" />
      </div>

      <HBar data={stats.by_project} title="Issues by project" colorVar="var(--color-background-warning)" />
    </div>
  );
}

// ─── Main AdminDashboard ──────────────────────────────────────────────────────

export function AdminDashboard({
  users, onCreateUser, onDeleteUser, isCreating, error, onRefreshUsers,
}: AdminDashboardProps) {
  const [activeTab, setActiveTab] = useState<NavTab>("users");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  async function handleLogout() {
    const { supabase } = await import("../lib/supabase");
    await supabase.auth.signOut();
    window.location.href = "/login";
  }

  return (
    <div style={{
      minHeight: "100vh",
      display: "flex",
      flexDirection: "column",
      background: "var(--color-background-tertiary)",
    }}>
      {/* Top bar */}
      <header style={{
        height: 56,
        background: "var(--color-background-primary)",
        borderBottom: "0.5px solid var(--color-border-tertiary)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "0 20px",
        position: "sticky", top: 0, zIndex: 10,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 30, height: 30, borderRadius: "var(--border-radius-md)",
            background: "var(--color-background-primary)",
            border: "0.5px solid var(--color-border-secondary)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 14, fontWeight: 700, color: "var(--color-text-primary)",
          }}>R</div>
          <span style={{ fontSize: 15, fontWeight: 500, color: "var(--color-text-primary)" }}>
            RedMind Admin
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{
            fontSize: 12, padding: "2px 8px",
            borderRadius: "var(--border-radius-md)",
            background: "var(--color-background-success)",
            color: "var(--color-text-success)",
            display: "inline-flex", alignItems: "center", gap: 4,
          }}>
            <CheckCircle2 size={11} /> Admin
          </span>
          <Button variant="ghost" size="sm" onClick={handleLogout} style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <LogOut size={14} /> Sign out
          </Button>
        </div>
      </header>

      {/* Body: sidebar + content */}
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>

        {/* Sidebar */}
        <aside style={{
          width: sidebarCollapsed ? 56 : 220,
          background: "var(--color-background-primary)",
          borderRight: "0.5px solid var(--color-border-tertiary)",
          display: "flex", flexDirection: "column",
          padding: "12px 0",
          transition: "width 0.2s ease",
          flexShrink: 0,
        }}>
          {NAV_ITEMS.map(item => {
            const active = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => setActiveTab(item.id)}
                title={sidebarCollapsed ? item.label : undefined}
                style={{
                  display: "flex", alignItems: "center",
                  gap: sidebarCollapsed ? 0 : 10,
                  padding: "10px 16px",
                  background: active ? "var(--color-background-secondary)" : "transparent",
                  border: "none", cursor: "pointer", width: "100%", textAlign: "left",
                  color: active ? "var(--color-text-primary)" : "var(--color-text-secondary)",
                  fontSize: 14, fontWeight: active ? 500 : 400,
                  borderLeft: active ? "2px solid var(--color-border-info)" : "2px solid transparent",
                  transition: "background 0.1s, color 0.1s",
                  overflow: "hidden",
                  whiteSpace: "nowrap",
                }}
              >
                <span style={{ flexShrink: 0, display: "flex" }}>{item.icon}</span>
                {!sidebarCollapsed && item.label}
              </button>
            );
          })}

          {/* Collapse toggle at bottom */}
          <div style={{ marginTop: "auto", padding: "0 8px" }}>
            <button
              onClick={() => setSidebarCollapsed(v => !v)}
              style={{
                width: "100%", background: "none", border: "none", cursor: "pointer",
                display: "flex", alignItems: "center", justifyContent: sidebarCollapsed ? "center" : "flex-end",
                padding: "8px",
                color: "var(--color-text-secondary)",
              }}
              title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            >
              {sidebarCollapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
            </button>
          </div>
        </aside>

        {/* Main content */}
        <main style={{
          flex: 1, padding: "2rem",
          overflow: "auto",
          maxWidth: 900,
        }}>
          {activeTab === "users" && (
            <UsersTab
              users={users}
              onCreateUser={onCreateUser}
              onDeleteUser={onDeleteUser}
              isCreating={isCreating}
              error={error}
            />
          )}
          {activeTab === "chatlogs" && <ChatLogsTab users={users} />}
          {activeTab === "stats" && <StatsTab />}
        </main>
      </div>
    </div>
  );
}