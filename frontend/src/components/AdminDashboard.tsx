import { useState } from "react";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Loader2, Trash2, Wifi, WifiOff } from "lucide-react";

interface User {
  id: string;
  email: string;
  full_name: string;
  is_redmine_connected: boolean;
}

interface AdminDashboardProps {
  users: User[];
  onCreateUser: (data: { email: string; password: string; full_name: string }) => void;
  onDeleteUser: (id: string) => void;
  isCreating: boolean;
  error: string | null;
}

export function AdminDashboard({ users, onCreateUser, onDeleteUser, isCreating, error }: AdminDashboardProps) {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    onCreateUser({ email, password, full_name: fullName });
    setFullName(""); setEmail(""); setPassword("");
  };

  async function handleLogout() {
    const { supabase } = await import("../lib/supabase");
    await supabase.auth.signOut();
    window.location.href = "/login";
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground text-sm font-bold">R</div>
            <h1 className="text-lg font-semibold text-card-foreground">RedMind Admin</h1>
          </div>
          <Button variant="ghost" size="sm" onClick={handleLogout}>
            Sign out
          </Button>
        </div>
      </div>

      <div className="mx-auto max-w-5xl space-y-8 px-6 py-8">
        <section className="space-y-3">
          <h2 className="text-base font-semibold text-foreground">Project Managers</h2>
          <div className="rounded-lg border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Full Name</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead>Redmine</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={4} className="text-center text-muted-foreground py-8">
                      No users yet.
                    </TableCell>
                  </TableRow>
                )}
                {users.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell className="font-medium">{user.full_name}</TableCell>
                    <TableCell className="text-muted-foreground">{user.email}</TableCell>
                    <TableCell>
                      {user.is_redmine_connected
                        ? <span className="flex items-center gap-1 text-green-600 text-sm"><Wifi className="h-3.5 w-3.5" />Connected</span>
                        : <span className="flex items-center gap-1 text-muted-foreground text-sm"><WifiOff className="h-3.5 w-3.5" />Not connected</span>
                      }
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="sm" variant="ghost"
                        className="text-destructive hover:text-destructive"
                        onClick={() => onDeleteUser(user.id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>

        <section className="space-y-3">
          <h2 className="text-base font-semibold text-foreground">Create New Project Manager</h2>
          <div className="rounded-lg border border-border bg-card p-6">
            <form onSubmit={handleCreate} className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <label className="text-sm font-medium">Full Name</label>
                <Input value={fullName} onChange={e => setFullName(e.target.value)} required disabled={isCreating} placeholder="John Doe" />
              </div>
              <div className="space-y-2">
                <label className="text-sm font-medium">Email</label>
                <Input type="email" value={email} onChange={e => setEmail(e.target.value)} required disabled={isCreating} placeholder="john@example.com" />
              </div>
              <div className="space-y-2 sm:col-span-2">
                <label className="text-sm font-medium">Password</label>
                <Input type="password" value={password} onChange={e => setPassword(e.target.value)} required disabled={isCreating} placeholder="••••••••" />
              </div>
              {error && <p className="text-sm text-destructive sm:col-span-2">{error}</p>}
              <div className="sm:col-span-2">
                <Button type="submit" disabled={isCreating}>
                  {isCreating ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Creating…</> : "Create"}
                </Button>
              </div>
            </form>
          </div>
        </section>
      </div>
    </div>
  );
}