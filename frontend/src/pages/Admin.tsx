import { useState, FormEvent } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Users,
  Bug,
  MessageSquare,
  LogOut,
  Plus,
  UserPlus,
} from "lucide-react";
import { useChatLogs, ChatLog } from "@/hooks/useChatLogs";

// ─── Mock stats (replace with real API calls) ───
const MOCK_STATS = {
  totalUsers: 12,
  totalIssues: 87,
  totalLogs: 243,
};

interface CreatedUser {
  email: string;
  createdAt: Date;
}

export default function Admin() {
  const { user, logout } = useAuth();
  const { logs } = useChatLogs();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<CreatedUser[]>([]);
  const [formMsg, setFormMsg] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  const handleCreateUser = async (e: FormEvent) => {
    e.preventDefault();
    setFormMsg(null);
    setCreating(true);
    try {
      const {
        data: { session },
      } = await supabase.auth.getSession();
      const res = await fetch("http://localhost:8000/admin/create-user", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${session?.access_token}`,
        },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) throw new Error(await res.text());
      setCreated((prev) => [{ email, createdAt: new Date() }, ...prev]);
      setFormMsg({
        type: "success",
        text: `User ${email} created as project_manager`,
      });
      setEmail("");
      setPassword("");
    } catch (err: any) {
      setFormMsg({ type: "error", text: err.message });
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      {/* Top bar */}
      <header className="sticky top-0 z-30 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
              <span className="font-bold text-primary-foreground text-sm">
                R
              </span>
            </div>
            <span className="font-semibold text-foreground">RedMind Admin</span>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-muted-foreground">{user?.email}</span>
            <Button variant="ghost" size="sm" onClick={() => logout()}>
              <LogOut className="h-4 w-4 mr-1" /> Logout
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl p-4 md:p-6 space-y-6">
        {/* ─── Stats Cards ─── */}
        <div className="grid gap-4 sm:grid-cols-3">
          <StatCard
            icon={<Users className="h-5 w-5" />}
            label="Total Users"
            value={MOCK_STATS.totalUsers + created.length}
          />
          <StatCard
            icon={<Bug className="h-5 w-5" />}
            label="Total Issues"
            value={MOCK_STATS.totalIssues}
          />
          <StatCard
            icon={<MessageSquare className="h-5 w-5" />}
            label="Chat Logs"
            value={MOCK_STATS.totalLogs + logs.length}
          />
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          {/* ─── Create User Form ─── */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-lg">
                <UserPlus className="h-5 w-5" /> Create Project Manager
              </CardTitle>
              <CardDescription>
                New user will be assigned the{" "}
                <code className="rounded bg-muted px-1 text-xs">
                  project_manager
                </code>{" "}
                role
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleCreateUser} className="space-y-4">
                {formMsg && (
                  <div
                    className={`rounded-lg px-3 py-2 text-sm ${
                      formMsg.type === "success"
                        ? "bg-green-50 text-green-700 border border-green-200"
                        : "bg-destructive/10 text-destructive border border-destructive/30"
                    }`}
                  >
                    {formMsg.text}
                  </div>
                )}
                <div className="space-y-2">
                  <Label htmlFor="newEmail">Email</Label>
                  <Input
                    id="newEmail"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="manager@company.com"
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="newPassword">Password</Label>
                  <Input
                    id="newPassword"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••"
                    required
                    minLength={6}
                  />
                </div>
                <Button type="submit" disabled={creating} className="w-full">
                  {creating ? (
                    "Creating…"
                  ) : (
                    <>
                      <Plus className="h-4 w-4 mr-1" /> Create User
                    </>
                  )}
                </Button>
              </form>

              {created.length > 0 && (
                <div className="mt-4 space-y-1">
                  <p className="text-xs font-medium text-muted-foreground">
                    Recently created:
                  </p>
                  {created.map((u, i) => (
                    <div
                      key={i}
                      className="flex items-center justify-between rounded bg-muted px-2 py-1 text-xs"
                    >
                      <span>{u.email}</span>
                      <span className="text-muted-foreground">
                        {u.createdAt.toLocaleTimeString()}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* ─── Chat Logs ─── */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-lg">
                <MessageSquare className="h-5 w-5" /> Recent Chat Logs
              </CardTitle>
              <CardDescription>
                Messages from all project managers
              </CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <div className="max-h-80 overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>User</TableHead>
                      <TableHead>Message</TableHead>
                      <TableHead>Time</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {logs.length === 0 ? (
                      <TableRow>
                        <TableCell
                          colSpan={3}
                          className="text-center text-muted-foreground py-8"
                        >
                          No chat logs yet
                        </TableCell>
                      </TableRow>
                    ) : (
                      logs.map((log) => (
                        <TableRow key={log.id}>
                          <TableCell className="text-xs">
                            {log.userEmail}
                          </TableCell>
                          <TableCell className="text-xs max-w-[200px] truncate">
                            {log.message}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                            {log.timestamp.toLocaleString()}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>
        </div>
      </main>
    </div>
  );
}

function StatCard({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
}) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 p-5">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          {icon}
        </div>
        <div>
          <p className="text-2xl font-bold text-foreground">{value}</p>
          <p className="text-sm text-muted-foreground">{label}</p>
        </div>
      </CardContent>
    </Card>
  );
}
