import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { supabase } from "../lib/supabase";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Loader2, CheckCircle2 } from "lucide-react";

export function SetRedmineKeyPage() {
  const navigate = useNavigate();
  const [key, setKey] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsLoading(true);
    setError("");
    setSuccess(null);

    try {
      const res = await api.post("/profile/redmine-key", { redmine_api_key: key });
      const { display_name } = res.data;
      setSuccess(`Connected as ${display_name}`);
      // Short delay so user sees the success message
      setTimeout(() => navigate("/chat"), 1500);
    } catch (err: any) {
      setError(err.response?.data?.detail ?? "Something went wrong. Please try again.");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleLogout() {
    await supabase.auth.signOut();
    navigate("/login");
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-6 rounded-lg border border-border bg-card p-8 shadow-sm">
        <div className="flex items-center justify-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground text-sm font-bold">R</div>
          <h1 className="text-xl font-semibold text-card-foreground">RedMind</h1>
        </div>

        <div className="space-y-1 text-center">
          <p className="font-medium text-card-foreground">Connect your Redmine account</p>
          <p className="text-sm text-muted-foreground">
            Enter your personal Redmine API key.<br />
            Find it in <strong>Redmine → My account → API access key</strong>.
          </p>
        </div>

        {success ? (
          <div className="flex flex-col items-center gap-3 py-2">
            <CheckCircle2 className="h-8 w-8 text-green-500" />
            <p className="text-sm font-medium text-green-600">{success}</p>
            <p className="text-xs text-muted-foreground">Redirecting to chat…</p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-card-foreground">Redmine API Key</label>
              <Input
                value={key}
                onChange={e => setKey(e.target.value)}
                placeholder="abc123..."
                required
                disabled={isLoading}
              />
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="w-full" disabled={isLoading || !key}>
              {isLoading
                ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Verifying…</>
                : "Save & Continue"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="w-full text-muted-foreground"
              onClick={handleLogout}
            >
              Sign out
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}