import { useEffect, useState } from "react";
import { supabase } from "../lib/supabase";
import type { Session, User } from "@supabase/supabase-js";

interface AuthState {
  session: Session | null;
  user: User | null;
  role: string | null;
  loading: boolean;
}

async function fetchRole(userId: string): Promise<string | null> {
  try {
    const { data } = await Promise.race([
      supabase.from("profiles").select("role").eq("id", userId).single(),
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("timeout")), 5000)
      ),
    ]);
    return (data as any)?.role ?? null;
  } catch {
    return null;
  }
}

export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({
    session: null,
    user: null,
    role: null,
    loading: true,
  });

  useEffect(() => {
    let isMounted = true;

    // Subscribe FIRST, before getSession
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      async (_event, session) => {
        if (!isMounted) return;
        const role = session?.user ? await fetchRole(session.user.id) : null;
        if (isMounted) {
          setState({ session, user: session?.user ?? null, role, loading: false });
        }
      }
    );

    // Then trigger an initial session check
    supabase.auth.getSession().then(async ({ data: { session } }) => {
      if (!isMounted) return;
      const role = session?.user ? await fetchRole(session.user.id) : null;
      if (isMounted) {
        setState({ session, user: session?.user ?? null, role, loading: false });
      }
    });

    return () => {
      isMounted = false;
      subscription.unsubscribe();
    };
  }, []);

  return state;
}