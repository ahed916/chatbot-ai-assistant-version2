import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { Login } from "../components/Login";
import { api } from "../api/client";

export function LoginPage() {
  const navigate = useNavigate();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(email: string, password: string) {
    setIsLoading(true);
    setError("");

    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) { setError(error.message); setIsLoading(false); return; }

    const { data: profile } = await supabase
      .from("profiles")
      .select("role, is_redmine_connected")
      .eq("id", data.user.id)
      .single();

    setIsLoading(false);
    const role = profile?.role;

    if (role === "admin") {
      navigate("/admin");
    } else if (role === "project_manager") {
      if (profile?.is_redmine_connected) {
        navigate("/chat");
      } else {
        navigate("/setup-redmine-key");
      }
    } else {
      setError("No role assigned. Contact your administrator.");
    }
  }

  return <Login onSubmit={handleSubmit} isLoading={isLoading} error={error} />;
}