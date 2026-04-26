import { useState } from "react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Loader2 } from "lucide-react";

interface LoginProps {
  onSubmit: (email: string, password: string) => void;
  isLoading: boolean;
  error: string;
}

export function Login({ onSubmit, isLoading, error }: LoginProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit(email, password);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-6 rounded-lg border border-border bg-card p-8 shadow-sm">
        <div className="flex items-center justify-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground text-sm font-bold">
            R
          </div>
          <h1 className="text-xl font-semibold text-card-foreground">RedMind</h1>
        </div>
        <p className="text-center text-sm text-muted-foreground">Sign in to your account</p>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <label htmlFor="email" className="text-sm font-medium text-card-foreground">Email</label>
            <Input id="email" type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required disabled={isLoading} />
          </div>
          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium text-card-foreground">Password</label>
            <Input id="password" type="password" placeholder="••••••••" value={password} onChange={(e) => setPassword(e.target.value)} required disabled={isLoading} />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button type="submit" className="w-full" disabled={isLoading}>
            {isLoading ? (<><Loader2 className="mr-2 h-4 w-4 animate-spin" />Signing in…</>) : "Sign in"}
          </Button>
        </form>
      </div>
    </div>
  );
}