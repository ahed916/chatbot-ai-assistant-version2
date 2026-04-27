import { useEffect, useState, FormEvent } from "react";
import { Plus, Trash2, UserPlus, CheckCircle2, XCircle, Mail, Loader2 } from "lucide-react";
import { adminApi, PMUser } from "@/lib/adminApi";
import { AdminLayout } from "@/components/admin/AdminLayout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";

export default function UsersPage() {
  const { toast } = useToast();
  const [users, setUsers] = useState<PMUser[]>([]);
  const [loading, setLoading] = useState(true);

  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [creating, setCreating] = useState(false);

  // api interceptor attaches the Supabase token automatically
  const reload = () => {
    setLoading(true);
    adminApi.listUsers()
      .then(setUsers)
      .finally(() => setLoading(false));
  };

  useEffect(reload, []);

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreating(true);
    try {
      await adminApi.createUser({ email, password, full_name: fullName });
      toast({ title: "User created", description: `${email} can now sign in as a project manager.` });
      setEmail("");
      setPassword("");
      setFullName("");
      setOpen(false);
      reload();
    } catch (err) {
      toast({
        title: "Failed to create user",
        description: (err as Error).message,
        variant: "destructive",
      });
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: string, userEmail: string) => {
    try {
      await adminApi.deleteUser(id);
      toast({ title: "User deleted", description: `${userEmail} has been removed.` });
      reload();
    } catch (err) {
      toast({
        title: "Delete failed",
        description: (err as Error).message,
        variant: "destructive",
      });
    }
  };

  return (
    <AdminLayout
      title="Project Managers"
      description="Create, view and remove project manager accounts"
      actions={
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button size="sm">
              <Plus className="h-4 w-4 mr-1.5" /> Add user
            </Button>
          </DialogTrigger>
          <DialogContent>
            <form onSubmit={handleCreate}>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-lg">
                  <UserPlus className="h-5 w-5 text-primary" /> New project manager
                </DialogTitle>
                <DialogDescription>
                  They will receive the{" "}
                  <code className="text-xs bg-muted rounded px-1">project_manager</code> role and
                  can sign in immediately.
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-4 py-4">
                <div className="space-y-2">
                  <Label htmlFor="fullName">Full name</Label>
                  <Input
                    id="fullName"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    placeholder="Jane Doe"
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="email">Email</Label>
                  <Input
                    id="email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="jane@company.com"
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="password">Temporary password</Label>
                  <Input
                    id="password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="Min. 8 characters"
                    minLength={8}
                    required
                  />
                </div>
              </div>
              <DialogFooter>
                <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={creating}>
                  {creating ? (
                    <><Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> Creating…</>
                  ) : (
                    "Create user"
                  )}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      }
    >
      <div className="rounded-xl border bg-card overflow-hidden">
        {/* Table header */}
        <div className="grid grid-cols-12 gap-4 px-5 py-3 border-b bg-muted/40 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          <div className="col-span-4">User</div>
          <div className="col-span-3">Email</div>
          <div className="col-span-2">Redmine</div>
          <div className="col-span-2">Created</div>
          <div className="col-span-1 text-right">Actions</div>
        </div>

        {loading ? (
          <div className="p-5 space-y-3">
            {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
          </div>
        ) : users.length === 0 ? (
          <div className="p-12 text-center">
            <UserPlus className="h-10 w-10 text-muted-foreground/50 mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">
              No project managers yet. Click <strong>Add user</strong> to create one.
            </p>
          </div>
        ) : (
          <ul className="divide-y">
            {users.map((u) => (
              <li
                key={u.id}
                className="grid grid-cols-12 gap-4 px-5 py-3.5 items-center hover:bg-muted/30 transition-colors"
              >
                {/* Avatar + name */}
                <div className="col-span-4 flex items-center gap-3 min-w-0">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary text-sm font-semibold">
                    {u.full_name?.[0]?.toUpperCase() ?? u.email[0]?.toUpperCase()}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-foreground truncate">{u.full_name || "—"}</p>
                    <p className="text-xs text-muted-foreground">Project Manager</p>
                  </div>
                </div>

                {/* Email */}
                <div className="col-span-3 text-sm text-foreground/80 truncate flex items-center gap-1.5">
                  <Mail className="h-3.5 w-3.5 text-muted-foreground" />
                  {u.email}
                </div>

                {/* Redmine status */}
                <div className="col-span-2">
                  {u.is_redmine_connected ? (
                    <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700 bg-emerald-500/10 px-2 py-0.5 rounded-full">
                      <CheckCircle2 className="h-3 w-3" /> Connected
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground bg-muted px-2 py-0.5 rounded-full">
                      <XCircle className="h-3 w-3" /> Not linked
                    </span>
                  )}
                </div>

                {/* Created date */}
                <div className="col-span-2 text-xs text-muted-foreground">
                  {new Date(u.created_at).toLocaleDateString()}
                </div>

                {/* Delete */}
                <div className="col-span-1 flex justify-end">
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-muted-foreground hover:text-destructive"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>Delete {u.full_name || u.email}?</AlertDialogTitle>
                        <AlertDialogDescription>
                          This will permanently remove the user account and revoke access. This
                          action cannot be undone.
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          onClick={() => handleDelete(u.id, u.email)}
                        >
                          Delete
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </AdminLayout>
  );
}